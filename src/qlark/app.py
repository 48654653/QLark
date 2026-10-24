"""应用装配层：把存储、日志、NapCat 管理、界面桥接到一起。"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import paths
from .bot import BotService
from .chat.memes import MemeLibrary
from .chat.memes import resolve_dir as resolve_meme_dir
from .chat.personas import PersonaError
from .chat.reply_format import FORMAT_REMINDER, build_output_rules, build_preview
from .chat.store import MessageStore
from .chat.summarizer import SUMMARY_MAX_CHARS
from .core.models import AppState, Phase
from .fun import config as fun_config
from .fun.weather import QWeather, WeatherError
from .logging_setup import setup_logging
from .napcat import netconfig
from .napcat.envcheck import EnvChecker, detect_credential_accounts
from .napcat.logbus import LogBus
from .napcat.login import LoginError, LoginManager
from .settings import Settings
from .storage.db import Database
from .version import WINDOW_TITLE
from . import model_config

log = logging.getLogger(__name__)

#: 每次推送最多带多少行日志（超出的留到下一拍，避免 evaluate_js 卡顿）
LOG_PUSH_BATCH = 300
#: 打开界面时预填的历史日志行数
LOG_BACKFILL = 300
#: 无变化时状态推送的心跳间隔（用于刷新运行时长）
STATE_HEARTBEAT = 3.0

#: 记忆轮数 W 的可选范围（需求规格 §6.2）
MEMORY_ROUNDS_RANGE = (1, 200)
#: 上下文预算的可选范围（字符数，粗估）
CONTEXT_BUDGET_RANGE = (2000, 200_000)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


class Application:
    """QLark 主应用。"""

    def __init__(self, *, debug: bool = False) -> None:
        self.debug = debug
        paths.ensure_data_dirs()

        self.db = Database(paths.db_path())
        self.settings = Settings(self.db)
        self.logbus = LogBus(capacity=5000, log_dir=paths.log_dir())
        setup_logging(self.logbus, debug=debug)

        log.info("QLark 启动，数据目录：%s", paths.data_dir())
        log.info("程序目录：%s", paths.app_dir())

        self.login = LoginManager(
            self.settings,
            self.logbus,
            on_change=self.mark_dirty,
            on_ready=self._on_login_ready,
            on_stopped=self._on_login_stopped,
        )
        self.bot = BotService(
            self.settings, self.db, self.logbus, on_change=self.mark_dirty
        )

        self._window: Any = None
        self._dirty = threading.Event()
        self._shutdown_event = threading.Event()
        self._pump_thread: threading.Thread | None = None
        self._last_log_seq = 0
        self._last_state_push = 0.0
        self._auto_started = False

    # ================================================================== 状态
    def state(self) -> dict[str, Any]:
        snap = self.login.snapshot()
        stats = self.db.stats_today()
        app_state = AppState(
            phase=snap["phase"],
            qq=snap["qq"] or self.settings.qq_account,
            nickname=snap["nickname"] or self.settings.qq_nickname,
            online=bool(snap["online"]),
            onebot_ready=bool(snap["onebot_ready"]),
            napcat_running=bool(snap["napcat_running"]),
            pid=snap["pid"],
            uptime_sec=int(snap["uptime_sec"]),
            qrcode=snap["qrcode"],
            last_error=snap["last_error"],
            theme=self.settings.theme,
            restart_required=self.settings.restart_required,
            stats=stats,
            env=snap["env"],
        )
        payload = app_state.to_dict()
        payload.update(self.bot.snapshot())
        return payload

    # ================================================================== 登录联动
    def _on_login_ready(self) -> None:
        """登录就绪 → 打开事件通道，开始收发消息。"""
        self.bot.start()
        self.mark_dirty()

    def _on_login_stopped(self) -> None:
        """NapCat 停了 → 机器人也停，避免残留的事件服务占着端口。"""
        if self.bot.running:
            self.bot.stop(wait=False)
        self.mark_dirty()

    def set_paused(self, paused: bool) -> None:
        self.bot.set_paused(paused)
        self.mark_dirty()

    # ================================================================== M3：模型配置
    @property
    def personas(self):
        return self.bot.pipeline.personas

    def get_model_config(self) -> dict[str, Any]:
        result = model_config.snapshot(self.settings)
        # 哪些人设**覆盖**了分段/表情概率。
        #
        # 必须报出来：用户把全局设成 90%，但那个人设上存着 10%，那个会话就永远是
        # 10%，而界面上看不出来 —— 看起来就像"概率坏了"（真踩过）。
        result["chance_overrides"] = self._chance_overrides()
        return result

    def _chance_overrides(self) -> dict[str, list[dict[str, Any]]]:
        """列出压过全局的人设。只在**值确实不同**时才报，避免噪音。"""
        out: dict[str, list[dict[str, Any]]] = {"split_chance": [], "meme_chance": []}
        enabled_key = {"split_chance": "split_enabled", "meme_chance": "meme_enabled"}
        for persona in self.personas.list_public():
            if not persona.get("enabled"):
                continue
            for key, flag in enabled_key.items():
                value = persona.get(key)
                if value is None or not persona.get(flag):
                    continue
                try:
                    percent = round(float(value) * 100)
                except (TypeError, ValueError):
                    continue
                if percent != round(float(self.settings.get(key) or 0) * 100):
                    out[key].append(
                        {"uid": persona.get("uid"), "name": persona.get("name"), "percent": percent}
                    )
        return out

    def apply_model_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = model_config.apply_model(self.settings, payload or {})
        if result.get("ok"):
            # 配置变了就让缓存的客户端失效
            self.bot.pipeline.invalidate_clients()
        self.mark_dirty()
        return result

    def apply_napcat_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = model_config.apply_napcat(self.settings, payload or {})
        self.mark_dirty()
        return result

    def test_model(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return model_config.test_model(self.settings, payload or {})

    # ---------------------------------------------------- 表情目录（分段/表情包）
    def _meme_library(self, path: str = "") -> MemeLibrary:
        return MemeLibrary(resolve_meme_dir(path or self.settings.meme_dir, paths.app_dir() / "memes"))

    def scan_meme_dir(self, path: str = "") -> dict[str, Any]:
        """扫描表情目录，返回情绪清单。

        界面用它做"配好了没"的即时反馈：路径填错、目录是空的、图片格式不支持，
        都能立刻看出来，而不是等到机器人该发表情时才发现。
        """
        library = self._meme_library(path)
        catalog = library.catalog()
        return {
            "ok": True,
            "path": str(library.root or ""),
            "exists": library.available,
            "emotions": catalog,
            "count": len(catalog),
            "total": sum(catalog.values()),
            "message": library.describe(),
        }

    def choose_meme_dir(self) -> dict[str, Any]:
        """弹系统文件夹选择框。用户取消时返回空字符串（不是错误）。

        pywebview 的 ``create_file_dialog`` 必须跑在**非创建窗口的那个线程**上；
        js_api 的方法本来就在独立线程里执行，所以这里直接调是安全的。
        """
        window = self._window
        if window is None:
            return {"ok": False, "path": "", "error": "界面还没就绪，请手动填写路径"}

        start = ""
        library = self._meme_library()
        if library.root and library.root.is_dir():
            start = str(library.root)
        try:
            import webview  # 延迟导入：headless 模式不需要它

            result = window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=start or str(paths.app_dir()),
            )
        except Exception as exc:  # pragma: no cover - 依赖真实 GUI
            log.warning("打开文件夹选择框失败：%s", exc)
            return {"ok": False, "path": "", "error": f"打开文件夹选择框失败：{exc}"}

        if not result:
            return {"ok": True, "path": "", "cancelled": True}
        selected = result[0] if isinstance(result, (list, tuple)) else result
        return {"ok": True, "path": str(selected), "cancelled": False}

    def preview_prompt(
        self, uid: int, split_prompt: str = "", meme_prompt: str = ""
    ) -> dict[str, Any]:
        """预览"即将发给 AI 的全部内容"。

        v2 起**没有概率机制**了：每一轮都会注入【输出规则】和末尾的格式提醒，
        要不要配表情、要不要发语音由模型自己判断。所以这里按"最完整的那种情况"
        拼出来 —— 用户点进来就是想看规则到底写了什么。

        ``split_prompt`` / ``meme_prompt`` 只为兼容旧的前端调用保留，**已经不起作用**
        （那两套自定义提示词在 v2 里不存在了）。

        注意「长期记忆摘要」是**每个会话一份**的，预览里拿不到具体会话，
        所以放一条说明占位，不能凭空造一个假的摘要出来骗人。
        """
        _ = (split_prompt, meme_prompt)
        persona = self.personas.get(int(uid))
        if persona is None:
            return {"ok": False, "error": f"人设 {uid} 不存在"}

        library = self._meme_library()
        emotions = library.emotions() if persona.get("meme_enabled") else []
        voice_on = bool(persona.get("voice_enabled")) and bool(
            self.settings.get("voice_enabled", False)
        )
        try:
            max_messages = int(self.settings.get("split_max_messages") or 3)
        except (TypeError, ValueError):
            max_messages = 3
        try:
            voice_max_chars = int(self.settings.get("voice_max_chars") or 20)
        except (TypeError, ValueError):
            voice_max_chars = 20

        rules = build_output_rules(
            emotions=emotions or ["（表情目录里没有图片）"],
            max_messages=max_messages,
            voice=voice_on,
            voice_max_chars=voice_max_chars,
        )

        note = self._preview_note(persona, voice_on, max_messages)

        blocks_and_note = build_preview(
            system_prompt=str(persona.get("system_prompt") or ""),
            rules=rules,
            enabled_note=note,
            reminder=FORMAT_REMINDER if rules else "",
        )
        return {"ok": True, **blocks_and_note}

    def _preview_note(
        self, persona: dict[str, Any], voice_on: bool, max_messages: int
    ) -> str:
        """预览里那段"实际运行时是什么样"的说明。"""
        meme_on = bool(persona.get("meme_enabled"))
        split_on = bool(persona.get("split_enabled"))
        keepalive_on = bool(self.settings.get("keepalive_enabled", True))
        try:
            keepalive_sec = int(self.settings.get("keepalive_seconds") or 60)
        except (TypeError, ValueError):
            keepalive_sec = 60

        lines = [
            "实际运行时**每一轮**都会注入【输出规则】和末尾的格式提醒 ——",
            "v2 起没有掷骰子了，要不要配表情、发语音、分几条都由模型自己判断。",
            "",
            f"· 配表情：{'开启（可选表情有 ' + str(len(self._meme_library().emotions())) + ' 种）' if meme_on else '未开启'}",
            f"· 发语音：{'开启（最多 ' + str(self._voice_max_chars()) + ' 字）' if voice_on else '未开启'}",
            f"· 分段：{'最多 ' + str(max_messages) + ' 条' if split_on else '未开启'}",
        ]
        if keepalive_on:
            lines.append(
                f"· @ 保活：开启（被 @ 之后 {keepalive_sec} 秒内不用再 @，"
                "只对那个人有效）"
            )
        else:
            lines.append("· @ 保活：未开启（每条都要 @）")
        return "\n".join(lines)

    def _voice_max_chars(self) -> int:
        try:
            return int(self.settings.get("voice_max_chars") or 20)
        except (TypeError, ValueError):
            return 20

    # ================================================================== M3：账号
    def list_accounts(self) -> dict[str, Any]:
        credentials = set(detect_credential_accounts())
        known = set(netconfig.detect_accounts())
        current = self.settings.qq_account
        accounts = [
            {
                "qq": qq,
                "nickname": self.settings.qq_nickname if qq == current else None,
                "has_credential": qq in credentials,
                "known_by_qlark": qq in known,
                "current": qq == current,
            }
            for qq in sorted(credentials | known)
        ]
        return {"current": current, "accounts": accounts}

    def login_as(self, qq: str) -> dict[str, Any]:
        try:
            self.login.switch_account(str(qq))
        except LoginError as exc:
            return {"ok": False, "error": str(exc)}
        self.mark_dirty()
        return {"ok": True}

    # ================================================================== M3：人设
    def list_personas(self) -> dict[str, Any]:
        from .chat.personas import DEFAULT_PERSONA_UID

        return {
            "default_uid": DEFAULT_PERSONA_UID,
            "personas": self.personas.list_public(),
        }

    def create_persona(self) -> dict[str, Any]:
        try:
            result = self.personas.create()
        except PersonaError as exc:
            return {"ok": False, "error": str(exc)}
        self.mark_dirty()
        return result

    def update_persona(self, uid: int, fields: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self.personas.update(int(uid), fields or {})
        except PersonaError as exc:
            return {"ok": False, "error": str(exc)}
        self.mark_dirty()
        return result

    def delete_persona(self, uid: int) -> dict[str, Any]:
        try:
            result = self.personas.delete(int(uid))
        except PersonaError as exc:
            return {"ok": False, "error": str(exc)}
        self.logbus.add(
            f"已删除人设 {uid}：{result.affected_sessions} 个会话回落到人设1，"
            f"删除 {result.deleted_messages} 条记忆",
            level="warn",
        )
        self.mark_dirty()
        return result.to_dict()

    def reset_persona_memory(self, uid: int) -> dict[str, Any]:
        try:
            result = self.personas.clear_memory(int(uid))
        except PersonaError as exc:
            return {"ok": False, "error": str(exc)}
        self.logbus.add(f"已清空人设 {uid} 的记忆（{result.get('deleted_messages')} 条）", level="warn")
        self.mark_dirty()
        return result

    def get_persona_permission(self) -> dict[str, Any]:
        return {
            "whitelist_only": bool(self.settings.get("persona_whitelist_only", True)),
            "whitelist": self.personas.whitelist(),
        }

    def set_persona_permission(self, whitelist_only: bool) -> dict[str, Any]:
        self.settings.set("persona_whitelist_only", bool(whitelist_only))
        self.mark_dirty()
        return {"ok": True, "whitelist_only": bool(whitelist_only)}

    def add_persona_whitelist(self, qq: str) -> dict[str, Any]:
        try:
            whitelist = self.personas.add_whitelist(qq)
        except PersonaError as exc:
            return {"ok": False, "error": str(exc)}
        self.mark_dirty()
        return {"ok": True, "whitelist": whitelist}

    def remove_persona_whitelist(self, qq: str) -> dict[str, Any]:
        whitelist = self.personas.remove_whitelist(qq)
        self.mark_dirty()
        return {"ok": True, "whitelist": whitelist}

    # ================================================================== 记忆（M4）
    @property
    def memory(self) -> MessageStore:
        """记忆读写门面。

        刻意复用流水线里那一个实例：另开一个 MessageStore 虽然也能读，
        但会让"面板里删掉的记忆"和"流水线正在用的记忆"变成两套认知。
        """
        return self.bot.pipeline.store

    def get_memory_config(self) -> dict[str, Any]:
        return {
            "memory_rounds": int(self.settings.get("memory_rounds") or 20),
            "auto_summary": bool(self.settings.get("auto_summary", True)),
            "summary_model": str(self.settings.get("summary_model") or ""),
            "max_context_chars": int(self.settings.get("max_context_chars") or 24000),
            "summary_runs": int(self.settings.get("summary_runs") or 0),
            "summary_max_chars": SUMMARY_MAX_CHARS,
            "overview": self.memory.memory_overview(),
        }

    def apply_memory_config(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        errors: dict[str, str] = {}
        updates: dict[str, Any] = {}

        if "memory_rounds" in payload:
            value = _as_int(payload["memory_rounds"])
            low, high = MEMORY_ROUNDS_RANGE
            if value is None:
                errors["memory_rounds"] = (
                    "记忆轮数不能为空" if _is_blank(payload["memory_rounds"]) else "记忆轮数必须是整数"
                )
            elif not low <= value <= high:
                errors["memory_rounds"] = f"记忆轮数必须在 {low}~{high} 之间"
            else:
                updates["memory_rounds"] = value

        if "auto_summary" in payload:
            updates["auto_summary"] = bool(payload["auto_summary"])

        if "summary_model" in payload:
            updates["summary_model"] = str(payload["summary_model"] or "").strip()

        if "max_context_chars" in payload:
            value = _as_int(payload["max_context_chars"])
            low, high = CONTEXT_BUDGET_RANGE
            if value is None:
                errors["max_context_chars"] = (
                    "上下文预算不能为空" if _is_blank(payload["max_context_chars"]) else "上下文预算必须是整数"
                )
            elif not low <= value <= high:
                errors["max_context_chars"] = f"上下文预算必须在 {low}~{high} 之间"
            else:
                updates["max_context_chars"] = value

        if errors:
            return {"ok": False, "errors": errors}

        if updates:
            self.settings.update(**updates)
            # 记忆轮数与压缩阈值是同一个值，改了要立刻生效
            self.bot.pipeline.invalidate_clients()
            self.logbus.add(
                "已应用记忆配置：" + "、".join(sorted(updates)), level="info"
            )
        self.mark_dirty()
        return {"ok": True, **self.get_memory_config()}

    def list_memories(self) -> dict[str, Any]:
        return {
            "ok": True,
            "memories": self.memory.list_memories(),
            "overview": self.memory.memory_overview(),
        }

    def get_memory(self, key: str) -> dict[str, Any]:
        key = str(key or "")
        if not key:
            return {"ok": False, "error": "缺少记忆键"}
        summary = self.memory.summary(key)
        return {
            "ok": True,
            "memory_key": key,
            "summary": (summary or {}).get("content") or "",
            "summary_pending": bool(summary and summary.get("pending")),
            "summary_updated_at": int((summary or {}).get("updated_at") or 0),
            "messages": self.memory.memory_messages(key),
        }

    def update_memory_message(self, message_id: int, content: str) -> dict[str, Any]:
        text = str(content or "")
        if not text.strip():
            return {"ok": False, "error": "内容不能为空"}
        if not self.memory.update_message(int(message_id), text):
            return {"ok": False, "error": "这条记忆不存在（可能已被删除）"}
        self.logbus.add(f"已修改记忆 #{int(message_id)}", level="info")
        self.mark_dirty()
        return {"ok": True, "id": int(message_id), "content": text}

    def delete_memory_message(self, message_id: int) -> dict[str, Any]:
        if not self.memory.delete_message(int(message_id)):
            return {"ok": False, "error": "这条记忆不存在（可能已被删除）"}
        self.logbus.add(f"已删除记忆 #{int(message_id)}", level="warn")
        self.mark_dirty()
        return {"ok": True}

    def delete_memory_summary(self, key: str) -> dict[str, Any]:
        key = str(key or "")
        if not self.memory.delete_summary(key):
            return {"ok": False, "error": "这个会话还没有总结"}
        self.logbus.add(f"已删除长期记忆摘要：{key}（原文已恢复为未压缩）", level="warn")
        self.mark_dirty()
        return {"ok": True}

    def clear_memory(self, key: str) -> dict[str, Any]:
        key = str(key or "")
        removed = self.memory.clear(key)
        self.logbus.add(f"已清空记忆：{key}（{removed} 条原文 + 总结）", level="warn")
        self.mark_dirty()
        return {"ok": True, "deleted_messages": removed}

    def summarize_memory_now(self, key: str) -> dict[str, Any]:
        """手动触发一次压缩（面板上的「立即压缩」）。

        **手动不受 W 轮门槛约束**：只要还有完整的问答轮次就压 —— 用户点按钮
        就是明确的"现在就压"。以前这里也走 W 门槛，未压缩 27 轮、门槛 30 时
        点下去**毫无反应**，用户只会以为自动压缩坏了（v2.2.1 修的）。
        自动路径仍然严格按 W 触发，见 ``Summarizer.maybe_summarize``。
        """
        key = str(key or "")
        outcome = self.bot.pipeline.summarizer.maybe_summarize(key, force=True)
        if outcome.did_call_model:
            self.db.bump_stats(
                prompt_tokens=outcome.prompt_tokens,
                completion_tokens=outcome.completion_tokens,
                calls=1,
            )
        self.mark_dirty()
        window = self.bot.pipeline.summarizer.window
        labels = {
            "ok": f"已压缩 {outcome.rounds} 轮 → {outcome.chars} 字",
            "not_enough": (
                f"没有可压缩的完整问答轮次（未压缩 {outcome.rounds} 轮）"
                if outcome.rounds == 0
                else f"未压缩 {outcome.rounds} 轮（不满 {window} 轮），手动压缩会按现有轮次直接压"
            ),
            "disabled": "自动总结已关闭",
            "failed": f"压缩失败，原文已保留：{outcome.error}",
            "empty": "模型返回了空摘要，原文已保留",
        }
        ok = outcome.status == "ok"
        if outcome.status == "failed":
            self.logbus.add(labels["failed"], level="warn")
        return {
            "ok": ok,
            "status": outcome.status,
            "rounds": outcome.rounds,
            "chars": outcome.chars,
            "message": labels.get(outcome.status, outcome.status),
        }

    # ================================================================== 趣味功能（v2.3）
    def get_fun_config(self) -> dict[str, Any]:
        """趣味功能的配置（和风密钥只回显打码值）。"""
        return fun_config.snapshot(self.settings)

    def apply_fun_config(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """保存趣味功能配置。

        校验失败的字段**不落库**：用户 Host 填错时，原来能用的 Host 不会被覆盖。
        """
        result = fun_config.apply(self.settings, payload or {})
        if result.get("ok"):
            self.mark_dirty()
            enabled = "已开启" if result.get("fun_enabled") else "已关闭"
            self.logbus.add(f"趣味功能{enabled}（默认城市：{result.get('qweather_city')}）")
        return result

    def test_fun_weather(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """用表单里的值（没填就用已保存的）真打一次和风，**不写库**。

        面板上的「测试连接」走这里。任何失败都返回 ``ok=False`` + 人话，
        绝不抛异常 —— 一个测试按钮不该让整个界面报错。
        """
        params = fun_config.resolve(self.settings, payload or {})
        if not params["host"]:
            return {"ok": False, "error": "还没填和风 API Host（控制台「设置」里那一行）"}
        if not params["key"]:
            return {"ok": False, "error": "还没填和风 API Key"}

        started = time.monotonic()
        client: QWeather | None = None
        try:
            client = QWeather(params["host"], params["key"])
            text = client.brief(params["city"])
        except WeatherError as exc:
            message = str(exc)
            self.logbus.add(f"天气测试失败：{message}", level="warn")
            return {"ok": False, "error": message}
        except Exception as exc:  # noqa: BLE001 - 测试按钮绝不冒泡
            self.logbus.add(f"天气测试异常：{exc}", level="error")
            return {"ok": False, "error": f"天气查询异常：{exc}"}
        finally:
            if client is not None:
                client.close()

        latency_ms = int((time.monotonic() - started) * 1000)
        self.logbus.add(f"天气测试成功：{text}（{latency_ms}ms）")
        return {"ok": True, "text": text, "city": params["city"], "latency_ms": latency_ms}

    def mark_dirty(self) -> None:
        self._dirty.set()

    # ================================================================== 首启向导（M6）
    def get_wizard_state(self) -> dict[str, Any]:
        """首启向导的状态与各步完成情况。

        每一步都用**已有的真实状态**判断，而不是另存一份"向导进度"：
        另存进度会和实际配置漂移（用户直接去模型页填好了，向导还显示没配）。
        """
        model = self.get_model_config()
        model_ready = bool(
            str(model.get("base_url") or "").strip()
            and str(model.get("model") or "").strip()
            and model.get("api_key_set")
        )
        personas = self.list_personas().get("personas") or []
        # 人设1 是出厂默认且不可删除，所以"人设可用"等价于"至少有一个启用中的人设"
        persona_ready = any(bool(item.get("enabled")) for item in personas)
        logged_in = bool(self.settings.qq_account)

        return {
            "first_run_done": bool(self.settings.get("first_run_done", False)),
            "risk_acknowledged": bool(self.settings.get("risk_acknowledged", False)),
            "steps": {
                "risk": bool(self.settings.get("risk_acknowledged", False)),
                "model": model_ready,
                "login": logged_in,
                "persona": persona_ready,
            },
            "detail": {
                "phase": self.login.phase.value,
                "qq": self.settings.qq_account,
                "model": str(model.get("model") or ""),
                "base_url": str(model.get("base_url") or ""),
                "persona_count": len(personas),
            },
        }

    def acknowledge_risk(self) -> dict[str, Any]:
        """用户确认"知道 QQ 机器人有风控 / 封号风险"。"""
        self.settings.risk_acknowledged = True
        self.logbus.add("用户已确认风控提示", level="info")
        self.mark_dirty()
        return {"ok": True, **self.get_wizard_state()}

    def finish_wizard(self) -> dict[str, Any]:
        """结束首启向导。**未确认风控时拒绝** —— 那是这一步存在的唯一理由。"""
        if not bool(self.settings.get("risk_acknowledged", False)):
            return {"ok": False, "error": "请先阅读并确认风控提示"}
        self.settings.first_run_done = True
        self.logbus.add("首启向导已完成", level="info")
        self.mark_dirty()
        return {"ok": True, **self.get_wizard_state()}

    def restart_wizard(self) -> dict[str, Any]:
        """重新走一遍向导（帮助菜单里的入口，也方便演示）。"""
        self.settings.first_run_done = False
        self.mark_dirty()
        return {"ok": True, **self.get_wizard_state()}

    # ================================================================== 推送
    def attach_window(self, window: Any) -> None:
        self._window = window

    def _eval(self, script: str) -> None:
        window = self._window
        if window is None:
            return
        try:
            window.evaluate_js(script)
        except Exception as exc:  # 界面还没就绪时调用会失败，忽略即可
            log.debug("evaluate_js 失败：%s", exc)

    def push_state(self) -> None:
        payload = json.dumps(self.state(), ensure_ascii=False)
        self._eval(f"window.qlark && window.qlark.onState({payload})")
        self._last_state_push = time.monotonic()

    def push_logs(self) -> None:
        lines = self.logbus.since(self._last_log_seq, limit=LOG_PUSH_BATCH)
        if not lines:
            return
        self._last_log_seq = lines[-1].seq
        payload = json.dumps([line.to_dict() for line in lines], ensure_ascii=False)
        self._eval(f"window.qlark && window.qlark.onLog({payload})")

    def _pump_loop(self) -> None:
        """定时器线程：批量推送日志与状态。"""
        while not self._shutdown_event.wait(0.25):
            try:
                self.push_logs()
                if self._dirty.is_set():
                    self._dirty.clear()
                    self.push_state()
                elif time.monotonic() - self._last_state_push > STATE_HEARTBEAT:
                    if self.login.is_running:
                        self.push_state()
            except Exception:
                log.exception("状态推送异常")

    def _start_pump(self) -> None:
        if self._pump_thread is not None and self._pump_thread.is_alive():
            return
        self._shutdown_event.clear()
        self._pump_thread = threading.Thread(
            target=self._pump_loop, name="qlark-pump", daemon=True
        )
        self._pump_thread.start()

    def _on_window_loaded(self) -> None:
        """界面 DOM 就绪：回填历史日志并推送一次完整状态。"""
        tail = self.logbus.tail(LOG_BACKFILL)
        if tail:
            self._last_log_seq = tail[-1].seq
            payload = json.dumps([line.to_dict() for line in tail], ensure_ascii=False)
            self._eval(f"window.qlark && window.qlark.onLog({payload})")
        self.push_state()
        self._auto_start()

    def _auto_start(self) -> None:
        """双击即开：界面就绪后自动拉起 NapCat 并完成登录。"""
        if self._auto_started:
            return
        self._auto_started = True

        def worker() -> None:
            time.sleep(1.0)
            self.start_napcat()

        threading.Thread(target=worker, name="qlark-autostart", daemon=True).start()

    # ================================================================== 动作
    def start_napcat(self) -> None:
        if self.login.is_running:
            self.logbus.add("NapCat 已在运行，忽略启动请求", level="debug")
            return
        self.login.start()

    def stop_napcat(self) -> None:
        self.login.stop(wait=False)
        self.mark_dirty()

    def restart_napcat(self) -> None:
        self.logbus.add("正在重启 NapCat…", level="info")
        self.login.restart()

    def relogin(self) -> None:
        self.logbus.add("已清除本地账号记录，将重新扫码登录", level="warn")
        self.login.relogin()

    def logout(self) -> None:
        self.login.logout()

    def run_env_check(self) -> dict[str, Any]:
        report = self.login.run_env_check(with_ports=not self.login.is_running)
        self.mark_dirty()
        return report.to_dict()

    def get_logs(self, since_seq: int = 0) -> dict[str, Any]:
        start = int(since_seq or 0)
        lines = self.logbus.since(start, limit=LOG_PUSH_BATCH)
        next_seq = lines[-1].seq if lines else start
        dropped = 0
        if start and lines and lines[0].seq > start + 1:
            # 客户端落后太多，缓冲里最早的序号已经被挤掉了
            dropped = lines[0].seq - start - 1
        return {"lines": [line.to_dict() for line in lines], "next": next_seq, "dropped": dropped}

    def clear_logs(self) -> None:
        self.logbus.clear()
        self._last_log_seq = self.logbus.latest_seq
        self.logbus.add("日志已清空", level="info")

    def export_logs(self) -> str:
        """导出全部日志文本（前端触发下载）。"""
        return self.logbus.export_text()

    def set_theme(self, theme: str) -> None:
        self.settings.theme = theme
        self.mark_dirty()

    # ================================================================== 系统
    def _open_path(self, target: Path, *, create: bool = False) -> None:
        try:
            if create:
                target.mkdir(parents=True, exist_ok=True)
            if target.exists():
                os.startfile(str(target))  # type: ignore[attr-defined]  # noqa: S606
            else:
                self.logbus.add(f"路径不存在：{target}", level="warn")
        except Exception as exc:
            self.logbus.add(f"打开路径失败：{exc}", level="error")

    def open_data_dir(self) -> None:
        self._open_path(paths.data_dir(), create=True)

    def open_runtime_dir(self) -> None:
        self._open_path(paths.napcat_dir())

    def open_log_dir(self) -> None:
        self._open_path(paths.log_dir(), create=True)

    # ================================================================== 生命周期
    def run(self, *, on_ready: Callable[[Any], None] | None = None) -> int:
        """启动图形界面（阻塞直到窗口关闭）。

        Args:
            on_ready: 可选的窗口就绪回调，收到 ``window`` 对象。供自动化脚本
                （截图、界面自检）在真实后端下驱动界面使用。
        """
        import webview  # 延迟导入：headless 模式不需要它

        index = paths.resolve_web_index()
        if not index.is_file():
            raise FileNotFoundError(f"找不到前端入口文件：{index}")

        from .api import JsApi

        api = JsApi(self)
        window = webview.create_window(
            WINDOW_TITLE,
            url=index.as_uri(),
            js_api=api,
            width=1180,
            height=780,
            min_size=(1000, 660),
            background_color="#12141a",
            text_select=True,
        )
        self.attach_window(window)
        window.events.loaded += self._on_window_loaded
        if on_ready is not None:
            window.events.loaded += lambda: on_ready(window)
        self._start_pump()

        log.info("界面已启动：%s", index)
        # private_mode=False：保留 localStorage，主题选择才能被记住
        webview.start(debug=self.debug, private_mode=False)

        self.shutdown()
        return 0

    def request_quit(self) -> None:
        """前端请求退出程序。

        M1 的关闭行为是"退出程序并停掉 NapCat"（安全、可预期）。
        按需求规格 v1.1 §3.1 应改为"最小化到托盘"，托盘与右键菜单留待后续实现。
        """
        window = self._window
        if window is not None:
            try:
                window.destroy()
                return
            except Exception:
                log.exception("销毁窗口失败")
        self.shutdown()

    def shutdown(self) -> None:
        """退出：停掉 NapCat 与后台线程，关闭数据库。"""
        if self._shutdown_event.is_set():
            return
        log.info("QLark 正在退出…")
        self._shutdown_event.set()
        try:
            self.bot.stop(wait=True)
        except Exception:
            log.exception("停止机器人失败")
        try:
            self.login.stop(wait=True)
        except Exception:
            log.exception("停止 NapCat 失败")
        thread = self._pump_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        try:
            self.logbus.add("QLark 已退出", level="info")
            self.logbus.close()
        finally:
            self.db.close()

    # 便于测试直接注入
    def env_checker(self) -> EnvChecker:
        return EnvChecker(
            host=self.settings.onebot_host,
            port=self.settings.onebot_port,
            event_port=self.settings.event_port,
        )

    @property
    def phase(self) -> Phase:
        return self.login.phase
