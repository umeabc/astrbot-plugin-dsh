"""AstrBot ↔ DeepSeek Harness bridge.

Commands (default wake prefix "/"):

    /dsh <指令>          新建一条 DSH 会话并执行（或按当前模式继续）
    /dsh new <指令>      强制新建会话
    /dshls [页码]        按工作区列出会话，默认 15 条一页
    /dsh <序号>          切到列表里的某条会话；之后的 /dsh <指令> 继续它
    /dsh stop            取消正在执行的会话
    /dsh status          查看 DSH 桥接状态
    /dsh help            帮助

The answer is sent as one message whose first line carries the elapsed time.
Long tasks emit a periodic "已经深度求索了 …" note; a turn whose reply never
reached the bot is delivered later as a proactive message.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # loaded as a package (data.plugins.<name>.main)
    from .dsh_astrbot_client import BridgeClient, BridgeError
except ImportError:  # loaded as a loose module
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from dsh_astrbot_client import BridgeClient, BridgeError

try:
    from astrbot.api.all import MessageChain
except ImportError:  # pragma: no cover - older packaging layout
    from astrbot.core.message.message_event_result import MessageChain

try:
    from astrbot.api.star import register as _register_star
except ImportError:  # pragma: no cover - very old AstrBot
    _register_star = None

DEFAULTS: dict[str, Any] = {
    "transport": "direct",
    "api_url": "http://127.0.0.1:8399",
    "token": "",
    "relay_url": "",
    "relay_room": "",
    "relay_key": "",
    "timeout_seconds": 900,
    "max_chars": 1500,
    "progress_message": True,
    "progress_text": "🔍 深度求索中…",
    "progress_interval_minutes": 10,
    "notify_poll_seconds": 20,
}

HELP_TEXT = """DSH 指令（仅管理员）
/dsh <指令> —— 新建会话执行
/dsh new <指令> —— 强制新建会话
/dshls [页码] —— 按工作区列出会话（默认 15 条一页）
/dsh <序号> —— 切换到该会话（列表含桌面端已有会话，切过去即可继续）
/dsh stop —— 取消正在执行的会话
/dsh status —— 查看桥接状态
说明：长任务会定时提示进度；中途报错会写明原因；结果里的本机文件以「附件N：路径」列出，不上传。"""


def _config(config: Any) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    if isinstance(config, dict):
        for key, value in config.items():
            if key in merged and value is not None:
                merged[key] = value
    for key, fallback, minimum in (
        ("timeout_seconds", 900, 30),
        ("max_chars", 1500, 200),
        ("progress_interval_minutes", 10, 0),
        ("notify_poll_seconds", 20, 0),
    ):
        try:
            merged[key] = max(minimum, int(merged[key]))
        except (TypeError, ValueError):
            merged[key] = fallback
    merged["progress_message"] = bool(merged["progress_message"])
    return merged


def _elapsed(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m{rest:02d}s"


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    if hours > 0:
        return f"{hours} 时 {minutes} 分"
    if minutes > 0:
        return f"{minutes} 分"
    return f"{total} 秒"


def _ago(timestamp_ms: float) -> str:
    if not timestamp_ms:
        return "未知"
    seconds = max(0, int(time.time() - timestamp_ms / 1000))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def _split(text: str, limit: int) -> list[str]:
    """Split on line boundaries so long answers survive platform limits."""
    if limit <= 0 or len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk != ""]


def _render_attachments(items: Any) -> str:
    """附件1：路径 / 附件2：路径 — never the files themselves."""
    if not isinstance(items, list):
        return ""
    lines = []
    for index, item in enumerate(items, start=1):
        path = item.get("path") if isinstance(item, dict) else item
        if path:
            lines.append(f"附件{index}：{path}")
    return "\n".join(lines)


def _task_name(text: str) -> str:
    first = next((line.strip() for line in str(text).splitlines() if line.strip()), "")
    first = first.replace("`", "").strip()
    if first == "":
        return "当前任务"
    return first[:20] + ("…" if len(first) > 20 else "")


class DshBridgePlugin(Star):
    """Talk to a DeepSeek Harness on this machine through dsh-astrbot-bridge."""

    name = "astrbot_plugin_dsh"
    author = "umeabc"
    desc = "在聊天里远程调用本机 DeepSeek Harness 执行任务"
    version = "0.3.0"

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context, config)
        self.conf = _config(config)
        self._client: BridgeClient | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._poll_task: asyncio.Task | None = None
        # Only conversations that actually invoked a DSH command may receive pushes.
        self._origins: set[str] = set()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def initialize(self) -> None:
        self.ensure_poller()

    def ensure_poller(self) -> None:
        """Deliver answers for turns the bot gave up waiting on."""
        if int(self.conf["notify_poll_seconds"]) <= 0:
            return
        if self._poll_task is not None and not self._poll_task.done():
            return
        try:
            self._poll_task = asyncio.create_task(self._notify_loop())
        except RuntimeError:  # no running loop (unit tests)
            self._poll_task = None

    def accepts_push(self, chat_key: str) -> bool:
        """A push may only go back to a conversation that used a DSH command."""
        return chat_key not in ("", "default") and chat_key in self._origins

    async def _notify_loop(self) -> None:
        interval = max(5, int(self.conf["notify_poll_seconds"]))
        while True:
            await asyncio.sleep(interval)
            try:
                pending = await self.client.notifications()
            except BridgeError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - a poller must not die
                logger.debug(f"dsh notify poll failed: {error}")
                continue
            for note in pending:
                chat_key = str(note.get("chatKey") or "")
                # Pushes never leave the conversation that asked for them.
                if not self.accepts_push(chat_key):
                    continue
                lock = self._locks.get(chat_key)
                if lock is not None and lock.locked():
                    continue
                await self._push(chat_key, note)

    @staticmethod
    def sender_label(event: AstrMessageEvent) -> str:
        """A short "id" or "name(id)" the DSH session list can show."""
        try:
            name = (event.get_sender_name() or "").strip()
            sender_id = str(event.get_sender_id() or "").strip()
        except Exception:  # noqa: BLE001 - keep the turn going
            return ""
        if name and sender_id and name != sender_id:
            return f"{name}({sender_id})"[:80]
        return (name or sender_id)[:80]

    async def _push(self, chat_key: str, note: dict[str, Any]) -> None:
        title = str(note.get("title") or "DSH 任务")
        error = str(note.get("error") or "")
        body = str(note.get("text") or "").strip()
        attachments = _render_attachments(note.get("attachments"))

        header = f"📬 （{title}）的深度求索" + ("报错" if error and not body else "结果")
        parts = [header]
        if error:
            parts.append(f"会话中途报错，原因：{error}")
        if body:
            parts.append(body)
        if attachments:
            parts.append(attachments)

        limit = int(self.conf["max_chars"])
        separator = chr(10)
        chunks = _split(separator.join(parts), limit)
        for index, chunk in enumerate(chunks):
            if len(chunks) == 1:
                message = chunk
            else:
                message = f"[{index + 1}/{len(chunks)}]" + separator + chunk
            try:
                await self.context.send_message(chat_key, MessageChain().message(message))
            except Exception as error:  # noqa: BLE001 - platform may be gone
                logger.warning(f"cannot push DSH notification to {chat_key}: {error}")
                return

        # Only now is the answer really delivered; an ack keeps it from coming back.
        turn_id = str(note.get("turnId") or "")
        if turn_id:
            try:
                await self.client.ack(chat_key, turn_id)
            except Exception as error:  # noqa: BLE001 - ack is best effort
                logger.debug(f"cannot ack notification {turn_id}: {error}")

    # ------------------------------------------------------------------ #
    # plumbing
    # ------------------------------------------------------------------ #

    @property
    def client(self) -> BridgeClient:
        if self._client is None:
            self._client = BridgeClient(self.conf)
        return self._client

    def lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def is_admin(self, event: AstrMessageEvent) -> bool:
        """Admins only: DSH runs real commands on the host machine."""
        try:
            if callable(getattr(event, "is_admin", None)) and event.is_admin():
                return True
        except Exception:  # noqa: BLE001 - platform event may be unusual
            pass
        try:
            sender = str(event.get_sender_id())
            config = self.context.get_config() if hasattr(self.context, "get_config") else None
            admins = list((config or {}).get("admins_id", []) or [])
            return sender in {str(item) for item in admins}
        except Exception:  # noqa: BLE001 - fall back to "denied"
            return False

    @staticmethod
    def chat_key(event: AstrMessageEvent) -> str:
        key = getattr(event, "unified_msg_origin", "") or getattr(event, "session_id", "") or ""
        return str(key) or "default"

    @staticmethod
    def argument(event: AstrMessageEvent, names: tuple[str, ...] = ("dsh", "深度求索")) -> str:
        message = (event.get_message_str() or "").strip()
        parts = message.split(maxsplit=1)
        if not parts:
            return ""
        if parts[0].lstrip("/！!").lower() in names:
            return parts[1].strip() if len(parts) > 1 else ""
        return message

    @staticmethod
    def _halt(event: AstrMessageEvent) -> None:
        """Consume the event so the LLM pipeline does not answer it too."""
        stopper = getattr(event, "stop_event", None)
        if callable(stopper):
            try:
                stopper()
            except Exception:  # noqa: BLE001 - older AstrBot builds differ
                pass

    async def _reply(self, event: AstrMessageEvent, text: str):
        limit = int(self.conf["max_chars"])
        chunks = _split(text, limit)
        for index, chunk in enumerate(chunks):
            body = chunk if len(chunks) == 1 else f"[{index + 1}/{len(chunks)}]\n{chunk}"
            yield event.plain_result(body)

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #

    @filter.command("dsh")
    async def dsh(self, event: AstrMessageEvent):
        """远程调用本机 DeepSeek Harness"""
        self._halt(event)
        if not self.is_admin(event):
            async for result in self._reply(event, "⛔ 只有管理员可以使用 DSH 指令。"):
                yield result
            return
        self._origins.add(self.chat_key(event))
        self.ensure_poller()
        argument = self.argument(event)
        lowered = argument.lower()

        if argument == "":
            async for result in self._reply(event, HELP_TEXT):
                yield result
            return

        if lowered in {"help", "帮助", "?"}:
            async for result in self._reply(event, HELP_TEXT):
                yield result
            return

        if lowered in {"list", "ls", "列表"}:
            async for result in self._list(event, 1):
                yield result
            return

        if lowered in {"stop", "取消"}:
            async for result in self._stop(event):
                yield result
            return

        if lowered in {"status", "健康", "ping"}:
            async for result in self._status(event):
                yield result
            return

        if argument.isdigit():
            async for result in self._switch(event, int(argument)):
                yield result
            return

        if lowered == "new":
            async for result in self._reply(event, "用法：/dsh new <指令> —— 用一条新会话执行这条指令。"):
                yield result
            return

        if lowered.startswith("new "):
            async for result in self._run(event, argument[4:].strip(), new_session=True):
                yield result
            return

        async for result in self._run(event, argument, new_session=False):
            yield result

    @filter.command("dshls")
    async def dshls(self, event: AstrMessageEvent):
        """按工作区列出 DSH 会话：/dshls [页码]"""
        self._halt(event)
        if not self.is_admin(event):
            async for result in self._reply(event, "⛔ 只有管理员可以使用 DSH 指令。"):
                yield result
            return
        self._origins.add(self.chat_key(event))
        self.ensure_poller()
        argument = self.argument(event, names=("dshls",))
        page = int(argument) if argument.isdigit() else 1
        async for result in self._list(event, max(1, page)):
            yield result

    # ------------------------------------------------------------------ #
    # actions
    # ------------------------------------------------------------------ #

    async def _run(self, event: AstrMessageEvent, text: str, *, new_session: bool):
        key = self.chat_key(event)
        if not text:
            async for result in self._reply(event, "指令是空的。"):
                yield result
            return
        if self.conf["progress_message"]:
            yield event.plain_result(str(self.conf["progress_text"]))

        name = _task_name(text)
        interval = float(self.conf["progress_interval_minutes"]) * 60
        started = time.time()
        async with self.lock_for(key):
            task = asyncio.create_task(
                self.client.chat(
                    key,
                    text,
                    follow=not new_session,
                    new_session=new_session,
                    user=self.sender_label(event),
                )
            )
            try:
                while True:
                    done, _ = await asyncio.wait({task}, timeout=interval if interval > 0 else None)
                    if task in done:
                        break
                    yield event.plain_result(
                        f"（{name}）已经深度求索了 {_duration(time.time() - started)}…"
                    )
                data = task.result()
            except BridgeError as error:
                logger.warning(f"dsh bridge call failed: {error}")
                yield event.plain_result(f"❌ DSH 调用失败：{error}")
                return
            except asyncio.CancelledError:
                task.cancel()
                raise
            except Exception as error:  # noqa: BLE001 - never break the bot
                logger.exception(f"dsh bridge unexpected failure: {error}")
                yield event.plain_result(f"❌ DSH 调用异常：{error}")
                return

        turn_id = str(data.get("turnId") or "")
        if turn_id:
            try:
                await self.client.ack(key, turn_id)
            except BridgeError:
                pass
            except Exception as error:  # noqa: BLE001 - ack is best effort
                logger.debug(f"dsh ack failed: {error}")

        async for result in self._reply(event, self._format_result(data)):
            yield result

    @staticmethod
    def _format_result(data: dict[str, Any]) -> str:
        header = f"⏱ {_elapsed(float(data.get('durationMs', 0)) / 1000)}"
        if data.get("interrupted"):
            header += " · 已中断"
        error = str(data.get("error") or "")
        if error:
            header += f"\n会话中途报错，原因：{error}"
        body = str(data.get("text") or "").strip()
        if body == "":
            body = "（DSH 没有返回文本内容）" if error == "" else ""
        attachments = _render_attachments(data.get("attachments"))
        parts = [header]
        if body:
            parts.append(body)
        if attachments:
            parts.append(attachments)
        return "\n".join(parts)

    async def _list(self, event: AstrMessageEvent, page: int = 1):
        key = self.chat_key(event)
        try:
            data = await self.client.sessions(key, page=page)
        except BridgeError as error:
            async for result in self._reply(event, f"❌ 读取会话列表失败：{error}"):
                yield result
            return

        total = int(data.get("total") or 0)
        if total == 0:
            async for result in self._reply(event, "还没有 DSH 会话。发送 /dsh <指令> 开始第一条。"):
                yield result
            return

        pages = max(1, int(data.get("pages") or 1))
        current_page = int(data.get("page") or 1)
        position = int(data.get("offset") or 0)
        current = data.get("currentSession")

        lines = [f"📚 DSH 会话（共 {total} 条 · 第 {current_page}/{pages} 页）"]
        for group in data.get("workspaces") or []:
            label = group.get("label") or ""
            path = group.get("path") or ""
            if label and path and label != path:
                title = f"{label} · {path}"
            else:
                title = path or label or "(默认工作区)"
            lines.append(f"📁 {title}")
            for entry in group.get("sessions") or []:
                position += 1
                mark = "▶" if entry.get("id") == current else " "
                name = entry.get("title") or "(未命名)"
                turns = entry.get("turns") or 0
                bits = [f"{mark} {position}. {name}"]
                if turns:
                    bits.append(f"{turns} 轮")
                bits.append(_ago(entry.get("lastUsed", 0)))
                if entry.get("user"):
                    bits.append(str(entry["user"]))
                if entry.get("origin") == "desktop":
                    bits.append("桌面会话")
                if entry.get("external"):
                    bits.append("已在别处继续")
                lines.append(" · ".join(bits))
        mode = "继续当前会话" if data.get("mode") == "continue" else "新建会话"
        lines.append(f"当前模式：{mode}")
        if current_page < pages:
            lines.append(f"发送 /dshls {current_page + 1} 看下一页；/dsh <序号> 切换")
        else:
            lines.append("发送 /dsh <序号> 切换；/dsh <指令> 新建会话")
        async for result in self._reply(event, "\n".join(lines)):
            yield result

    async def _switch(self, event: AstrMessageEvent, index: int):
        key = self.chat_key(event)
        try:
            data = await self.client.switch(key, index=index)
        except BridgeError as error:
            message = str(error)
            if '超出范围' not in message:
                message = f'切换失败：{message}'
            async for result in self._reply(event, f'❌ {message}'):
                yield result
            return

        title = data.get('title') or ''
        workspace = data.get('workspace') or ''
        label = f'{index}. {title}' if title else f'{index}'
        extra = f'工作区：{workspace}' if workspace else ''
        message = f'✅ 已切换到会话 {label}'
        if extra:
            message += chr(10) + extra
        message += chr(10) + '之后的 /dsh <指令> 会在该会话里继续；/dsh new <指令> 另开一条。'
        async for result in self._reply(event, message):
            yield result

    async def _stop(self, event: AstrMessageEvent):
        try:
            data = await self.client.cancel(self.chat_key(event))
        except BridgeError as error:
            async for result in self._reply(event, f"❌ 取消失败：{error}"):
                yield result
            return
        text = "🛑 已请求取消当前会话。" if data.get("cancelled") else "当前没有正在执行的会话。"
        async for result in self._reply(event, text):
            yield result

    async def _status(self, event: AstrMessageEvent):
        try:
            data = await self.client.health()
        except BridgeError as error:
            async for result in self._reply(
                event,
                f"❌ 连接失败：{error}\n请确认 DSH 正在运行、插件已加载，并且 token / 地址正确。",
            ):
                yield result
            return
        relay = data.get("relay") or {}
        lines = [
            f"✅ DSH 桥接正常（{data.get('version', '?')}）",
            f"连接方式：{self.conf['transport']}",
            f"工作区：{data.get('workspace') or '(DSH 默认)'}",
            f"权限：{data.get('permission') or '(DSH 默认)'}",
            f"渠道：{data.get('provider') or '(桌面默认)'}",
            f"模型：{data.get('model') or '(桌面默认)'}",
            f"常驻会话：{data.get('liveSessions', 0)}",
            f"运行中：{len(data.get('running') or [])}",
            f"待送达：{data.get('pendingNotifications', 0)}",
            f"中转：{'已连接' if relay.get('connected') else ('未启用' if relay.get('enabled') is not True else '未连接')}",
        ]
        async for result in self._reply(event, "\n".join(lines)):
            yield result

    async def terminate(self) -> None:
        task = self._poll_task
        self._poll_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._client = None
        self._locks.clear()
        logger.info("dsh bridge plugin stopped")


if _register_star is not None:  # keep very old AstrBot installs loadable
    try:
        DshBridgePlugin = _register_star(
            "astrbot_plugin_dsh",
            DshBridgePlugin.author,
            DshBridgePlugin.desc,
            DshBridgePlugin.version,
        )(DshBridgePlugin)
    except Exception:  # noqa: BLE001 - metadata.yaml already covers modern installs
        pass
