"""活人感评测：自动喂一批场景，给机器人回复打分。

**为什么需要这个**：改提示词不能靠"我觉得好点了"。这个脚本把"像不像人"
拆成三个可自动计分的维度，改一次提示词就跑一遍看分数。

    python tools/eval_humanity.py
    python tools/eval_humanity.py --runs 3             # 每个场景跑 3 次
    python tools/eval_humanity.py --no-rubric          # 关掉判断表做对照
    python tools/eval_humanity.py --persona 17         # 换个角色评

三个维度（每项 0~2 分，每场景满分 6）：

    情绪匹配  —— 对方在情绪里时，回复必须认真、不能敷衍
    表演密度  —— 一条回复里塞了几个"自加戏"（口癖/动作描写/米饭梗）
    长度合理  —— 有没有超长演讲

评分是**启发式**的（数关键词），不是让模型自己评 —— 那样会既贵又不可复现。
分数只用来比较"改前 vs 改后"，不追求绝对正确。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qlark import paths  # noqa: E402
from qlark.chat.context import build_chat_messages  # noqa: E402
from qlark.chat.reply_format import REPLY_RUBRIC  # noqa: E402
from qlark.llm.client import LLMError, OpenAIChatClient  # noqa: E402
from qlark.settings import Settings  # noqa: E402
from qlark.storage.db import Database  # noqa: E402

#: 「自加戏」的词：这些出现得多，说明它在表演而不是在说话。
#:
#: 注意**不算**"本鲸/人家" —— 那是自称（代词），本来就得用。
#: 算的是那些**可以省略的**表演：口癖、动作描写、梗。
PERF_WORDS = ("哼", "米饭", "大米", "白饭", "喵～", "哼～")
#: 括号里的动作描写：（尾巴一甩）(歪头)
ACTION_RE = re.compile(r"[（(][^）)]{1,20}[）)]")
#: 明显的敷衍（整条回复只有这些）
LAZY_WORDS = ("嗯", "哦", "好", "行", "不知道", "？")
#: "接住了话"的标志：追问词。短回复里只要有这些，就说明它在认真接话，
#: 比如对"我被开除了"回一句"啊 怎么回事" —— 只有 6 个字，但完全不是敷衍。
INQUIRY_WORDS = (
    "？", "?", "啥", "什么", "怎么", "哪", "多少", "吗", "呢",
    "谁", "说说", "讲讲", "然后", "真的假的", "为啥",
)


@dataclass
class Scenario:
    """一个评测场景。"""

    kind: str
    text: str
    #: 情绪场景：回复至少要这么长（低于这个就是在敷衍）
    min_len: int = 0
    #: 长度上限（超过就是在演讲）
    max_len: int = 60

    @property
    def emotional(self) -> bool:
        return self.min_len > 0


#: 15 个场景，覆盖"该认真"和"该简短"两类 ——
#: 只测"能不能说短"是错的：敷衍一个正在情绪里的人是更严重的失败。
SCENARIOS: tuple[Scenario, ...] = (
    # ---- 情绪场景：必须认真，绝不能敷衍 ----
    Scenario("情绪求助", "求你了，不@他我就会死", min_len=8, max_len=60),
    Scenario("情绪攻击", "抽你妈", min_len=6, max_len=60),
    Scenario("情绪难过", "我今天被开除了", min_len=8, max_len=70),
    Scenario("情绪低落", "我有点想不开了", min_len=10, max_len=70),
    Scenario("情绪撒娇", "人家想你了", min_len=6, max_len=50),
    # ---- 日常场景：该短就短 ----
    Scenario("打招呼", "在吗", max_len=20),
    Scenario("问事实", "一加一等于几", max_len=20),
    Scenario("闲聊", "早上好", max_len=20),
    Scenario("单调回复", "嗯", max_len=20),
    Scenario("抱怨", "好累啊不想上班", max_len=40),
    Scenario("逗趣", "不玩不给你吃饭", max_len=40),
    Scenario("挑衅", "大肥鱼", max_len=40),
    Scenario("求夸奖", "我帅吗", max_len=30),
    Scenario("收尾", "算了不聊了，晚安", max_len=40),
    # ---- 超长诱饵：最容易诱发演讲 ----
    Scenario("超长风险", "给我讲讲你为什么叫鲸鱼娘，还有你喜欢什么，顺便说说你对人类的看法", max_len=90),
    # ---- 下面三个是"更狠"的场景，专挑模型容易翻车的地方 ----
    # 被冷落：真人会试着挽留，机器人往往就"好的"了
    Scenario("被冷落", "算了，跟你说也没用", min_len=6, max_len=60),
    # 前提错误：真人会纠正，机器人常常顺着错的前提答
    Scenario("错误前提", "你这只猫怎么老甩尾巴", max_len=40),
    # 追问同一个人：真人会记得上一句，机器人容易忘
    Scenario("话题延续", "刚才我跟你说的事你还记得吗", max_len=50),
)


def count_performance(text: str) -> int:
    """数一条回复里塞了几个"自加戏"。"""
    score = 0
    for word in PERF_WORDS:
        score += text.count(word)
    score += len(ACTION_RE.findall(text))
    return score


def is_lazy(text: str) -> bool:
    """整条回复是不是只有敷衍的一个词。"""
    stripped = re.sub(r"[\s。！!？?~～…]+", "", text)
    return bool(stripped) and stripped in LAZY_WORDS


def engages(text: str) -> bool:
    """这条回复有没有真的"接住话"（追问、追问式短句都算）。"""
    return any(word in text for word in INQUIRY_WORDS)


def score(reply: str, scenario: Scenario) -> tuple[int, int, int]:
    """返回 ``(情绪匹配, 表演密度, 长度合理)``，各 0~2。"""
    length = len(reply.strip())

    # ---- 情绪匹配 ----
    #
    # 不能只看字数：对"我被开除了"回一句"啊 怎么回事"只有 6 个字，
    # 但它是**接住了话**，不是敷衍。真正该判失败的是"嗯""哦""不知道"这种。
    if scenario.emotional:
        if is_lazy(reply) or (length < 5 and not engages(reply)):
            emotional = 0          # 敷衍情绪里的人 = 最严重的失败
        elif length >= scenario.min_len or engages(reply):
            emotional = 2
        else:
            emotional = 0
    else:
        # 日常场景不看情绪，给满分（长度分单独扣）
        emotional = 2

    # ---- 表演密度 ----
    perf = count_performance(reply)
    if perf <= 1:
        density = 2
    elif perf <= 2:
        density = 1
    else:
        density = 0

    # ---- 长度合理 ----
    if length <= scenario.max_len:
        fit = 2
    elif length <= scenario.max_len * 1.6:
        fit = 1
    else:
        fit = 0

    return emotional, density, fit


@dataclass
class Run:
    scenario: Scenario
    reply: str
    scores: tuple[int, int, int]
    seconds: float = 0.0

    @property
    def total(self) -> int:
        return sum(self.scores)


def load_context(
    persona_uid: int, memory_key: str = ""
) -> tuple[Settings, Database, str, list[tuple[str, str]]]:
    db = Database(paths.db_path())
    settings = Settings(db)
    row = db.query("SELECT system_prompt FROM personas WHERE uid = ?", (persona_uid,))
    persona = str(row[0]["system_prompt"] or "").strip() if row else ""

    # 历史默认取**消息最多的那个会话**，而不是写死某个号 ——
    # 写死的话会把使用者自己的 QQ 号留在代码里。
    if not memory_key:
        rows = db.query(
            "SELECT memory_key, COUNT(*) AS n FROM messages GROUP BY memory_key "
            "ORDER BY n DESC LIMIT 1"
        )
        memory_key = str(rows[0]["memory_key"]) if rows else ""

    history: list[tuple[str, str]] = []
    if memory_key:
        history = [
            (r["role"], r["content"])
            for r in db.query(
                "SELECT role, content FROM messages WHERE memory_key = ? "
                "ORDER BY id DESC LIMIT 8",
                (memory_key,),
            )
        ]
        history.reverse()
    return settings, db, persona, history


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QLark 活人感评测")
    parser.add_argument("--persona", type=int, default=1, help="评测哪个人设（uid）")
    parser.add_argument("--runs", type=int, default=1, help="每个场景跑几次")
    parser.add_argument("--no-rubric", action="store_true", help="不注入「回复判断表」（做对照）")
    parser.add_argument("--no-history", action="store_true", help="不带历史（排掉历史的影响）")
    parser.add_argument(
        "--memory-key",
        default="",
        help="用哪个会话的历史当背景；留空 = 自动取消息最多的那个会话",
    )
    parser.add_argument(
        "--prompt-file",
        default="",
        help="从这个文件读人设提示词，**不读数据库、也不写数据库** —— 用来先验证一份模板再决定要不要换上",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="打印每条回复全文")
    args = parser.parse_args(argv)

    settings, db, persona, history = load_context(args.persona, args.memory_key)
    if args.prompt_file:
        prompt_path = Path(args.prompt_file)
        if not prompt_path.is_file():
            print(f"[FAIL] 找不到提示词文件：{prompt_path}")
            return 1
        persona = prompt_path.read_text(encoding="utf-8").strip()
        print(f"（使用 {prompt_path.name} 里的提示词，{len(persona)} 字，不读写数据库）")
    if not persona:
        print(f"[FAIL] 人设 {args.persona} 不存在或提示词为空")
        return 1
    if not settings.get("api_key"):
        print("[FAIL] 没有配置 API Key")
        return 1

    client = OpenAIChatClient(
        str(settings.get("base_url") or ""),
        str(settings.get("api_key") or ""),
        str(settings.get("model") or ""),
        max_tokens=min(int(settings.get("max_tokens") or 400), 400),
        temperature=float(settings.get("temperature") or 1.0),
        thinking=str(settings.get("thinking_mode") or "off"),
    )
    client._session.trust_env = False

    rubric = "" if args.no_rubric else REPLY_RUBRIC
    use_history = [] if args.no_history else history

    print("=" * 92)
    print(f"人设 {args.persona} · 模型 {settings.get('model')} · 温度 {settings.get('temperature')}"
          f" · 判断表 {'关' if args.no_rubric else '开'}"
          f" · 历史 {'无' if args.no_history else f'{len(history)} 条'}"
          f" · 每场景 {args.runs} 次")
    print("=" * 92)

    runs: list[Run] = []
    for scenario in SCENARIOS:
        for index in range(args.runs):
            user = (
                f"昵称：毛糙蛋\n时间：2026-10-01 22:{index:02d}\n消息：{scenario.text}"
            )
            messages = build_chat_messages(
                system_prompt=persona,
                history=use_history,
                user_content=user,
                trailing_system=rubric,
            )
            started = time.monotonic()
            try:
                result = client.complete(messages)
                reply = result.text.strip()
            except LLMError as exc:
                print(f"  [{scenario.kind}] 调用失败：{exc}")
                continue
            elapsed = time.monotonic() - started
            runs.append(Run(scenario, reply, score(reply, scenario), elapsed))

    db.close()
    if not runs:
        print("\n[FAIL] 一次都没成功")
        return 1

    # ---- 明细 ----
    print()
    print(f"{'场景':<10}{'字数':>5}  {'情绪':>4}{'密度':>4}{'长度':>4}  {'合计':>4}  回复")
    print("-" * 92)
    for run in runs:
        e, d, f = run.scores
        flag = ""
        if e == 0:
            flag = "  ← 敷衍了情绪"
        elif run.total <= 3:
            flag = "  ← 偏假"
        reply = run.reply if args.verbose else run.reply[:34].replace("\n", " ")
        print(f"{run.scenario.kind:<10}{len(run.reply):>5}  {e:>4}{d:>4}{f:>4}  {run.total:>4}  {reply}{flag}")

    # ---- 汇总 ----
    total = sum(r.total for r in runs)
    full = len(runs) * 6
    emotional_runs = [r for r in runs if r.scenario.emotional]
    lazy_emotional = [r for r in emotional_runs if r.scores[0] == 0]
    long_runs = [r for r in runs if r.scores[2] == 0]
    dense_runs = [r for r in runs if r.scores[1] == 0]
    avg_seconds = sum(r.seconds for r in runs) / len(runs)

    print()
    print("=" * 92)
    print(f"  总分        {total}/{full}  ({total / full * 100:.0f}%)")
    print(f"  情绪匹配    {sum(r.scores[0] for r in runs)}/{len(runs) * 2}"
          f"   敷衍了情绪的 {len(lazy_emotional)}/{len(emotional_runs)} 条"
          f"{'  ← 最严重的问题' if lazy_emotional else ''}")
    print(f"  表演密度    {sum(r.scores[1] for r in runs)}/{len(runs) * 2}"
          f"   自加戏过多的 {len(dense_runs)} 条")
    print(f"  长度合理    {sum(r.scores[2] for r in runs)}/{len(runs) * 2}"
          f"   超长的 {len(long_runs)} 条")
    print(f"  平均耗时    {avg_seconds:.1f}s")
    print("=" * 92)
    lengths = sorted(len(r.reply) for r in runs)
    print(f"  字数分布：最短 {lengths[0]}  中位 {lengths[len(lengths) // 2]}  "
          f"平均 {sum(lengths) / len(lengths):.0f}  最长 {lengths[-1]}")
    return 0 if total / full >= 0.75 else 2


if __name__ == "__main__":
    sys.exit(main())
