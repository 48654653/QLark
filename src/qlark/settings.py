"""应用配置：默认值 + 类型化读写。

M1 阶段实际用到的只有账号、主题与 OneBot 连接参数；其余键先定义好，
避免后面改动读取代码。
"""

from __future__ import annotations

from typing import Any

from .storage.db import Database
from .version import (
    DEFAULT_EVENT_PORT,
    DEFAULT_ONEBOT_PORT,
    DEFAULT_ONEBOT_TOKEN,
)

#: 配置键 → 默认值
DEFAULTS: dict[str, Any] = {
    # ---- 账号 ----
    "qq_account": None,          # 上次成功登录的 QQ 号（客户端自己持久化，不依赖 NapCat 日志）
    "qq_nickname": None,
    # ---- 界面 ----
    "theme": "dark",
    # ---- OneBot 网络（写入 onebot11_<QQ>.json 时使用）----
    "onebot_host": "127.0.0.1",
    "onebot_port": DEFAULT_ONEBOT_PORT,
    "onebot_token": DEFAULT_ONEBOT_TOKEN,
    # ---- 事件通道（NapCat → QLark 的消息推送）----
    "event_host": "127.0.0.1",
    "event_port": DEFAULT_EVENT_PORT,
    # ---- 机器人行为（M2）----
    "bot_paused": False,
    "max_concurrent": 4,
    "dedup_window": 600,
    #: 群聊回复是否引用原消息。默认关：群里引用会让消息看着很吵，
    #: 而且 @ 机器人时本来就上下文清楚，不需要靠引用定位。
    "reply_quote_group": False,
    "max_reply_chars": 900,
    "notify_error_private": True,
    #: 消息是否包成「昵称 / 时间 / 消息」三行块（群聊与私聊都生效）。
    #: **强烈建议保持开启**：一个群共用一份记忆，不带昵称的话模型分不清
    #: 哪句是谁说的，会把 A 的偏好记到 B 头上；时间则让它能理解"早上好"
    #: 是几点的早上好、这句是不是隔了两小时才回。
    "message_label": True,
    "ignored_users": [],
    "ignored_groups": [],
    # ---- 记忆 / 人设（M4）----
    "memory_rounds": 20,
    "auto_summary": True,
    "persona_whitelist_only": True,
    #: 累计"已压缩次数"，只用于记忆面板的容量提示展示
    "summary_runs": 0,
    #: 单次请求的上下文预算（字符数，粗估）。超出就从**最早的原文**开始丢弃，
    #: 长期记忆摘要始终保留（需求规格 §6.3）
    "max_context_chars": 24000,
    #: 单条消息最多处理几张图（需求规格 §6.5「设张数上限」）
    "max_images_per_message": 3,
    #: 是否把图片交给视觉模型（关掉则一律降级成 [图片] 占位）
    "image_understanding": True,
    #: 是否把视频交给模型（走原生 ``video_url``，不下载不抽帧）。
    #: 关掉则一律降级成 [视频] 占位。
    "video_understanding": True,
    #: 带视频的请求超时（秒）。服务端要先下载解码整段视频，默认 60s 不够。
    "video_timeout_s": 180,

    # ---- 趣味功能（v2.3）----
    #
    # 总开关默认**关**：开着就意味着"把群友提到的城市名发给第三方"，
    # 这是用户该自己决定的事，不能默认替人做主。
    #: 趣味功能总开关（天气等外部数据能力）
    "fun_enabled": False,
    #: 和风天气的专属 API Host（形如 xxx.re.qweatherapi.com）。
    #: 公共域名（api/devapi.qweather.com）已返回 Invalid Host ——
    #: API Host 本身就是认证的一部分。
    "qweather_host": "",
    "qweather_key": "",
    #: 默认城市：开了功能但没填城市时用
    "qweather_city": "北京",

    # ---- 随机分段回复 / 表情包回复 ----
    #
    # 「会不会做」是人设级的（见 personas.split_enabled / meme_enabled），
    # 「做得多频繁」是全局调参 —— 换个人设不该把频率也换掉。
    #
    # **概率必须由代码掷骰子**：只要把规则发给模型，它几乎每次都会用。
    # 没掷中时那个规则块**根本不进提示词**，模型不知道有这个能力，自然不会用。
    #: 分段概率（0~1）
    "split_chance": 0.25,
    #: 一次回复最多拆成几条
    "split_max_messages": 2,
    #: 多条之间的间隔（毫秒）。瞬间连发一眼就是机器人。
    "split_delay_ms": 500,
    #: 表情包概率（0~1），与分段**相互独立**
    "meme_chance": 0.20,
    #: 表情目录；留空 = <程序目录>/memes
    "meme_dir": "",
    #: 输出格式不对时重新生成几次（1 次 = 最多调用模型 2 次）
    "reply_retry_max": 1,
    #: 重试用尽后的兜底文案
    "reply_fallback_text": "服务器宕机了喵",
    #: 是否注入「回复判断表」（教模型按情境判断该回多长、该不该认真）
    "reply_rubric_enabled": True,
    #: 要不要发 response_format={"type":"json_object"}。
    #:
    #: v2 每次都要 JSON，这是双保险的第一道（第二道是末尾的提醒）。
    #: MiMo 支持；DeepSeek 认这个字段但**带历史时会返回空内容** ——
    #: 客户端会检测到空内容并自动关掉重试，所以默认开着是安全的。
    "reply_json_mode": True,

    # ---- @ 激活保活（v2.1）----
    #
    # 被 @ 之后给他一段时间，期间他不必再 @ 也能接着聊；每说一句都续期。
    # **窗口按 (会话, 用户) 独立** —— 别人说话不能续期、也不能蹭用，
    # 否则群里一直有人说话窗口就永远不关，机器人会变成每句都插嘴。
    "keepalive_enabled": True,
    "keepalive_seconds": 60,

    # ---- 免@ 消息的合并（v2.2）----
    #
    # 有人习惯把一句话拆成好几条发（"在" "吗" "问个事"）。每条都回会变成
    # 「咋了 / 嗯？ / 说」三连，一眼就是机器人。所以：攒起来，静默后一起回。
    #   quiet：对方停下多久算"说完了"
    #   max  ：从第一条算起的硬上限，防止一直打字导致永远不回
    #
    # 为什么不用"每 15 秒轮询一次"：那样单条消息的延迟取决于它落在哪个格子里
    # （0～15 秒随机），同样的话忽快忽慢，比稳定偏慢更假。
    "coalesce_enabled": True,
    "coalesce_quiet_seconds": 3,
    "coalesce_max_seconds": 15,

    # ---- 引用消息（v2.1）----
    #
    # 别人引用一条消息再说话时，把被引用的内容取回来给模型看
    # （文字 / 图片 / 视频 / 转发的聊天记录都要能看到）。
    # OneBot 的 reply 段只带 ID，所以要额外调一次 get_msg。
    "quote_context_enabled": True,

    # ---- 语音回复（v2）----
    #
    # 没有骰子：要不要发由模型自己在 JSON 里填 voice。这里只管"能不能"和护栏。
    "voice_enabled": False,
    #: 语音的字数上限。超过就忽略模型的 voice，改发文字 ——
    #: 实测 46 字要念 21 秒，长语音没人愿意听。
    "voice_max_chars": 20,
    #: TTS 模型名。**共用对话的 base_url / api_key**，只单独配模型名。
    "tts_model": "mimo-v2.5-tts-voicedesign",
    #: 合成超时（秒）
    "tts_timeout_s": 20,
    #: 全局默认音色描述；人设上没写就用这个
    "voice_prompt": (
        "一个十三四岁少女的声音，清透干净，稚嫩里带着奶气，不带沙哑也不嗲。"
        "说话轻快，还带着点没长大的软。不用把情绪演出来。"
    ),

    # ---- 媒体上下文（v2）----
    #
    # 记忆里不放 [图片] 占位符，真实图片直接进上下文。
    # 实测 token ≈ 像素数 ÷ 1024：512px ≈ 258 token，很便宜。
    "media_history_rounds": 2,
    "image_max_side": 512,
    #: 模型思考强度：off / auto / low / medium / high / max（空串 = 不发送，跟随服务端默认）
    #:
    #: **默认 off**。实测 DeepSeek 的默认是「开思考」，而一次闲聊回复里有
    #: 86% 的输出 token 花在推理上；关掉之后输出 token 直接减半，成功率不变。
    #: 另外官方明确「temperature 在思考模式下无效」——不关掉的话温度设置是摆设。
    "thinking_mode": "off",
    #: 首启向导是否已完成（M6）
    "first_run_done": False,
    #: 用户是否已确认"QQ 机器人存在风控/封号风险"（需求规格 §9.3）
    "risk_acknowledged": False,
    #: 单会话每分钟最多回复几条（需求规格 §9.3「发送速率限制」）。0 = 不限制
    "rate_limit_per_minute": 12,
    #: 命中就拒绝回复的词（需求规格 §9.3「可选敏感词过滤」），空列表 = 关闭
    "blocked_words": [],
    # ---- 模型（M3 使用，先占位）----
    "base_url": "",
    "api_key": "",
    "model": "",
    "vision_model": "",
    "summary_model": "",
    "max_tokens": 2048,
    "temperature": 0.8,
    # ---- 运行态标记 ----
    "restart_required": False,   # 有 NapCat 侧配置待重启生效
}


class Settings:
    """基于 :class:`Database` 的类型化配置门面。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------ 通用
    def get(self, key: str, default: Any = None) -> Any:
        if key in DEFAULTS:
            fallback = DEFAULTS[key]
        else:
            fallback = default
        value = self._db.get_config(key, fallback)
        if value is None and fallback is not None:
            return fallback
        return value

    def set(self, key: str, value: Any) -> None:
        self._db.set_config(key, value)

    def update(self, **kwargs: Any) -> None:
        if kwargs:
            self._db.update_config(kwargs)

    def all(self) -> dict[str, Any]:
        merged = dict(DEFAULTS)
        merged.update(self._db.all_config())
        return merged

    # ------------------------------------------------------------- 通配属性
    #
    # 为什么需要这两个方法：下面那些 @property 只覆盖了一部分配置键。其余键
    # （memory_rounds / auto_summary / model / max_tokens …）如果直接写
    # `settings.memory_rounds = 3`，Python 会**悄悄建一个和配置表无关的实例属性**，
    # 而 get("memory_rounds") 仍然返回旧值 —— 赋值看起来成功了，其实完全没生效。
    # 这种静默失效极难排查，所以在这里统一路由到配置表。
    def __getattr__(self, name: str) -> Any:
        # 只在常规查找失败后才会走到这里；下划线开头直接放弃，避免 __init__ 期间递归
        if name.startswith("_"):
            raise AttributeError(name)
        if name in DEFAULTS:
            return self.get(name)
        raise AttributeError(f"未知配置项：{name}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        descriptor = getattr(type(self), name, None)
        if isinstance(descriptor, property):
            if descriptor.fset is None:
                raise AttributeError(f"{name} 是只读配置，请用 set() 或专用方法修改")
            descriptor.fset(self, value)
            return
        if name in DEFAULTS:
            self.set(name, value)
            return
        raise AttributeError(f"未知配置项：{name}")

    # ------------------------------------------------------------------ 快捷属性
    @property
    def qq_account(self) -> str | None:
        value = self.get("qq_account")
        return str(value) if value else None

    @qq_account.setter
    def qq_account(self, value: str | None) -> None:
        self.set("qq_account", str(value) if value else None)

    @property
    def qq_nickname(self) -> str | None:
        value = self.get("qq_nickname")
        return str(value) if value else None

    @qq_nickname.setter
    def qq_nickname(self, value: str | None) -> None:
        self.set("qq_nickname", value or None)

    @property
    def theme(self) -> str:
        value = str(self.get("theme") or "dark")
        return value if value in ("dark", "light") else "dark"

    @theme.setter
    def theme(self, value: str) -> None:
        self.set("theme", "light" if value == "light" else "dark")

    @property
    def onebot_host(self) -> str:
        return str(self.get("onebot_host") or "127.0.0.1")

    @onebot_host.setter
    def onebot_host(self, value: str) -> None:
        self.set("onebot_host", str(value))

    @property
    def onebot_port(self) -> int:
        try:
            return int(self.get("onebot_port"))
        except (TypeError, ValueError):
            return DEFAULT_ONEBOT_PORT

    @onebot_port.setter
    def onebot_port(self, value: int | str) -> None:
        try:
            self.set("onebot_port", int(value))
        except (TypeError, ValueError):
            self.set("onebot_port", DEFAULT_ONEBOT_PORT)

    @property
    def onebot_token(self) -> str:
        return str(self.get("onebot_token") or DEFAULT_ONEBOT_TOKEN)

    @onebot_token.setter
    def onebot_token(self, value: str) -> None:
        self.set("onebot_token", str(value))

    @property
    def onebot_base_url(self) -> str:
        return f"http://{self.onebot_host}:{self.onebot_port}"

    # ------------------------------------------------------------------ 事件通道
    @property
    def event_host(self) -> str:
        return str(self.get("event_host") or "127.0.0.1")

    @event_host.setter
    def event_host(self, value: str) -> None:
        self.set("event_host", str(value))

    @property
    def event_port(self) -> int:
        try:
            return int(self.get("event_port"))
        except (TypeError, ValueError):
            return DEFAULT_EVENT_PORT

    @event_port.setter
    def event_port(self, value: int | str) -> None:
        try:
            self.set("event_port", int(value))
        except (TypeError, ValueError):
            self.set("event_port", DEFAULT_EVENT_PORT)

    @property
    def event_base_url(self) -> str:
        return f"http://{self.event_host}:{self.event_port}"

    # ------------------------------------------------------------------ 机器人行为
    @property
    def bot_paused(self) -> bool:
        return bool(self.get("bot_paused"))

    @bot_paused.setter
    def bot_paused(self, value: bool) -> None:
        self.set("bot_paused", bool(value))

    @property
    def max_concurrent(self) -> int:
        try:
            return max(1, min(16, int(self.get("max_concurrent"))))
        except (TypeError, ValueError):
            return 4

    @max_concurrent.setter
    def max_concurrent(self, value: int | str) -> None:
        try:
            self.set("max_concurrent", max(1, min(16, int(value))))
        except (TypeError, ValueError):
            self.set("max_concurrent", 4)

    @property
    def dedup_window(self) -> int:
        try:
            return max(30, int(self.get("dedup_window")))
        except (TypeError, ValueError):
            return 600

    @dedup_window.setter
    def dedup_window(self, value: int | str) -> None:
        try:
            self.set("dedup_window", max(30, int(value)))
        except (TypeError, ValueError):
            self.set("dedup_window", 600)

    @property
    def reply_quote_group(self) -> bool:
        return bool(self.get("reply_quote_group", False))

    @reply_quote_group.setter
    def reply_quote_group(self, value: bool) -> None:
        self.set("reply_quote_group", bool(value))

    @property
    def message_label(self) -> bool:
        return bool(self.get("message_label", True))

    @message_label.setter
    def message_label(self, value: bool) -> None:
        self.set("message_label", bool(value))

    @property
    def max_reply_chars(self) -> int:
        try:
            return max(100, int(self.get("max_reply_chars")))
        except (TypeError, ValueError):
            return 900

    @max_reply_chars.setter
    def max_reply_chars(self, value: int | str) -> None:
        try:
            self.set("max_reply_chars", max(100, int(value)))
        except (TypeError, ValueError):
            self.set("max_reply_chars", 900)

    @property
    def notify_error_private(self) -> bool:
        return bool(self.get("notify_error_private", True))

    @notify_error_private.setter
    def notify_error_private(self, value: bool) -> None:
        self.set("notify_error_private", bool(value))

    # ------------------------------------------------- 随机分段 / 表情包（调参）
    def _clamped(self, key: str, default, *, low, high, cast):
        try:
            value = cast(self.get(key))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    @property
    def split_chance(self) -> float:
        return self._clamped("split_chance", 0.25, low=0.0, high=1.0, cast=float)

    @split_chance.setter
    def split_chance(self, value) -> None:
        self.set("split_chance", self._clamp_to(value, 0.0, 1.0, float, 0.25))

    @property
    def split_max_messages(self) -> int:
        return self._clamped("split_max_messages", 2, low=1, high=5, cast=int)

    @split_max_messages.setter
    def split_max_messages(self, value) -> None:
        self.set("split_max_messages", self._clamp_to(value, 1, 5, int, 2))

    @property
    def split_delay_ms(self) -> int:
        return self._clamped("split_delay_ms", 500, low=0, high=5000, cast=int)

    @split_delay_ms.setter
    def split_delay_ms(self, value) -> None:
        self.set("split_delay_ms", self._clamp_to(value, 0, 5000, int, 500))

    @property
    def meme_chance(self) -> float:
        return self._clamped("meme_chance", 0.20, low=0.0, high=1.0, cast=float)

    @meme_chance.setter
    def meme_chance(self, value) -> None:
        self.set("meme_chance", self._clamp_to(value, 0.0, 1.0, float, 0.20))

    @property
    def reply_retry_max(self) -> int:
        return self._clamped("reply_retry_max", 1, low=0, high=3, cast=int)

    @reply_retry_max.setter
    def reply_retry_max(self, value) -> None:
        self.set("reply_retry_max", self._clamp_to(value, 0, 3, int, 1))

    @property
    def meme_dir(self) -> str:
        return str(self.get("meme_dir") or "").strip()

    @meme_dir.setter
    def meme_dir(self, value) -> None:
        self.set("meme_dir", str(value or "").strip())

    @property
    def reply_fallback_text(self) -> str:
        return str(self.get("reply_fallback_text") or "").strip() or "服务器宕机了喵"

    @reply_fallback_text.setter
    def reply_fallback_text(self, value) -> None:
        self.set("reply_fallback_text", str(value or "").strip())

    @staticmethod
    def _clamp_to(value, low, high, cast, default):
        try:
            return max(low, min(high, cast(value)))
        except (TypeError, ValueError):
            return default

    def id_set(self, key: str) -> set[str]:
        raw = self.get(key)
        if not isinstance(raw, (list, tuple, set)):
            return set()
        return {str(item).strip() for item in raw if str(item).strip()}

    @property
    def restart_required(self) -> bool:
        return bool(self.get("restart_required"))

    @restart_required.setter
    def restart_required(self, value: bool) -> None:
        self.set("restart_required", bool(value))
