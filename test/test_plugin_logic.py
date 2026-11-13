"""Command-layer test for main.py without a live AstrBot.

Stubs the three astrbot modules main.py imports, feeds it a fake bridge client,
and checks the rendered replies (pagination, switching, timing header).

    python test/test_plugin_logic.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import logging
import sys
import types
from pathlib import Path

ROOT = Path(os.environ.get("DSH_PLUGIN_DIR", Path(__file__).resolve().parent.parent))

# --------------------------------------------------------------------------- #
# minimal astrbot surface
# --------------------------------------------------------------------------- #

astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")
api.logger = logging.getLogger("astrbot-test")

event_mod = types.ModuleType("astrbot.api.event")


class AstrMessageEvent:  # noqa: D101
    def __init__(self, message: str, key: str = "qq/group/1", role: str = "admin") -> None:
        self.message = message
        self.unified_msg_origin = key
        self.role = role

    def get_message_str(self) -> str:
        return self.message

    def stop_event(self) -> None:
        self.stopped = True

    def is_admin(self) -> bool:
        return self.role == "admin"

    def get_sender_id(self) -> str:
        return "10001"

    def get_sender_name(self) -> str:
        return "管理员"

    def plain_result(self, text: str):
        return ("text", text)


class _Filter:
    @staticmethod
    def command(name):
        def decorate(fn):
            fn.__command__ = name
            return fn

        return decorate


event_mod.AstrMessageEvent = AstrMessageEvent
event_mod.filter = _Filter()
event_mod.__all__ = ["AstrMessageEvent", "filter"]

star_mod = types.ModuleType("astrbot.api.star")


class Star:  # noqa: D101
    def __init__(self, context, config=None) -> None:
        self.context = context
        self.config = config


class Context:  # noqa: D101
    pass


class FakeContext(Context):  # noqa: D101
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        return True


star_mod.Star = Star
star_mod.Context = Context
star_mod.register = lambda *args, **kwargs: (lambda cls: cls)

all_mod = types.ModuleType("astrbot.api.all")


class MessageChain:  # noqa: D101
    def __init__(self) -> None:
        self.text = ""

    def message(self, text: str):  # noqa: D102
        self.text += text
        return self


all_mod.MessageChain = MessageChain
sys.modules["astrbot.api.all"] = all_mod
api.all = all_mod

sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = api
sys.modules["astrbot.api.event"] = event_mod
sys.modules["astrbot.api.star"] = star_mod
astrbot.api = api

spec = importlib.util.spec_from_file_location("dsh_plugin_main", ROOT / "main.py")
main = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(main)

# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.acks: list[tuple] = []
        self.pending: list[dict] = []
        self.answer = "答案：看一下 D 盘"
        self.error = ""
        self.attachments: list[dict] = []
        self.delay = 0.0
        self.page_data = {
            "total": 17,
            "page": 1,
            "pages": 2,
            "offset": 0,
            "perPage": 15,
            "mode": "new",
            "currentSession": "s1",
            "workspaces": [
                {
                    "path": "D:/harness",
                    "label": "harness",
                    "sessions": [
                        {"id": "s1", "title": "修复登录", "turns": 3, "lastUsed": 0},
                        {"id": "s2", "title": "看磁盘", "turns": 1, "lastUsed": 0},
                    ],
                },
                {
                    "path": "D:/work",
                    "label": "work",
                    "sessions": [
                        {"id": "s3", "title": "写周报", "turns": 2, "lastUsed": 0},
                        {"id": "s4", "title": "桌面上的会话", "turns": 0, "lastUsed": 0, "origin": "desktop"},
                    ],
                },
            ],
        }

    async def sessions(self, key, page=None, per_page=None):
        data = dict(self.page_data)
        data["page"] = page or 1
        data["offset"] = ((page or 1) - 1) * 15
        return data

    async def chat(self, key, text, **kwargs):
        self.calls.append(("chat", key, text, kwargs))
        await asyncio.sleep(self.delay)
        return {
            "text": self.answer,
            "durationMs": 12345,
            "sessionId": "s9",
            "turns": 1,
            "turnId": "turn-1",
            "error": self.error,
            "attachments": self.attachments,
        }

    async def ack(self, key, turn_id):
        self.acks.append((key, turn_id))
        return {"acked": 1}

    async def notifications(self):
        return self.pending

    async def switch(self, key, session_id="", index=None):
        self.calls.append(("switch", key, index))
        return {
            "sessionId": "s2abcdef1234",
            "mode": "continue",
            "index": index,
            "title": "看磁盘",
            "workspace": "D:/harness",
        }

    async def cancel(self, key):
        return {"cancelled": False}

    async def health(self):
        return {
            "version": "0.1.0",
            "workspace": "D:/harness",
            "permission": "danger-full-access",
            "provider": "openai-codex",
            "model": "gpt-6.1-sol",
            "liveSessions": 2,
            "running": [],
            "relay": {"enabled": True, "connected": True},
        }


async def collect(generator):
    return [item[1] async for item in generator]


async def main_checks() -> int:
    failures = 0

    def check(label, condition, detail=None):
        nonlocal failures
        if condition:
            print(f"  ok   {label}")
        else:
            failures += 1
            print(f"  FAIL {label} :: {detail!r}")

    plugin = main.DshBridgePlugin(Context(), {"max_chars": 4000, "progress_message": True})
    plugin._client = FakeClient()

    event = AstrMessageEvent("/dsh 看一下 D 盘")
    replies = await collect(plugin.dsh(event))
    check("progress message comes first", replies[0] == "🔍 深度求索中…", replies)
    check("result carries the elapsed line first", replies[1].startswith("⏱ 12.3s\n"), replies[1])
    check("result carries the answer", "答案：看一下 D 盘" in replies[1], replies[1])
    check("chat was called with follow", plugin._client.calls[-1][3].get("follow") is True, plugin._client.calls[-1])

    replies = await collect(plugin.dsh(AstrMessageEvent("/dsh new 新话题")))
    check("new forces a new session", plugin._client.calls[-1][3].get("new_session") is True, plugin._client.calls[-1])

    listing = await collect(plugin.dshls(AstrMessageEvent("/dshls")))
    text = listing[0]
    check("list shows the total and page", "共 17 条 · 第 1/2 页" in text, text)
    check("list groups by workspace", "📁 harness · D:/harness" in text and "📁 work · D:/work" in text, text)
    check("list numbers sessions globally", "▶ 1. 修复登录" in text and "  3. 写周报" in text, text)
    check("desktop sessions are marked", "4. 桌面上的会话" in text and "桌面会话" in text, text)
    check("list offers the next page", "/dshls 2 看下一页" in text, text)

    listing = await collect(plugin.dshls(AstrMessageEvent("/dshls 2")))
    check("page 2 keeps numbering continuous", "📁 harness · D:/harness" in listing[0], listing[0])

    switched = await collect(plugin.dsh(AstrMessageEvent("/dsh 2")))
    check("switch asks by index", plugin._client.calls[-1] == ("switch", "qq/group/1", 2), plugin._client.calls[-1])
    check("switch reports the session name", "已切换到 2. 看磁盘" in switched[0], switched[0])
    check("switch reports the workspace", "工作区：D:/harness" in switched[0], switched[0])
    check(
        "unnamed sessions fall back to a short id",
        main.DshBridgePlugin._switched_label(4, {"sessionId": "abcdef123456"}) == "4. (未命名 · #abcdef12)",
        main.DshBridgePlugin._switched_label(4, {"sessionId": "abcdef123456"}),
    )

    status = await collect(plugin.dsh(AstrMessageEvent("/dsh status")))
    check("status shows the model", "gpt-6.1-sol" in status[0] and "openai-codex" in status[0], status[0])

    help_text = await collect(plugin.dsh(AstrMessageEvent("/dsh")))
    check("empty argument shows help", "/dshls" in help_text[0], help_text[0])
    check("help says admins only", "仅管理员" in help_text[0], help_text[0])

    before = len(plugin._client.calls)
    member = AstrMessageEvent("/dsh 看一下 D 盘", role="member")
    refused = await collect(plugin.dsh(member))
    check("non-admins are refused", "只有管理员" in refused[0], refused[0])
    check("non-admin never reaches the bridge", len(plugin._client.calls) == before, plugin._client.calls[-1])

    check("invoked origins are remembered", plugin.accepts_push("qq/group/1"), plugin._origins)
    check("unknown origins are not pushed", not plugin.accepts_push("qq/other/9"), plugin._origins)
    check("the default key is never pushed", not plugin.accepts_push("default"), plugin._origins)
    check("sender label is readable", plugin.sender_label(AstrMessageEvent("/dsh x")) == "管理员(10001)", plugin.sender_label(AstrMessageEvent("/dsh x")))
    await collect(plugin.dsh(AstrMessageEvent("/dsh 再看一次")))
    check("the caller is forwarded", plugin._client.calls[-1][3].get("user") == "管理员(10001)", plugin._client.calls[-1])

    # --- new behaviours ----------------------------------------------------
    ctx = FakeContext()
    slow = main.DshBridgePlugin(ctx, {"max_chars": 4000})
    slow.conf["progress_interval_minutes"] = 0.005  # 0.3 s between ticks
    slow._client = FakeClient()
    slow._client.delay = 0.8
    ticks = await collect(slow.dsh(AstrMessageEvent("/dsh 长任务")))
    check("progress ticks while waiting", any("已经深度求索了" in item for item in ticks), ticks)
    check("progress line names the task", any("（长任务）已经深度求索了" in item for item in ticks), ticks)
    check("the answer still arrives last", ticks[-1].startswith("⏱"), ticks[-1])
    check("ack is sent after the answer", slow._client.acks == [("qq/group/1", "turn-1")], slow._client.acks)

    errored = main.DshBridgePlugin(FakeContext(), {"progress_message": False})
    errored._client = FakeClient()
    errored._client.error = "429 Rate Limit Reached"
    failed = await collect(errored.dsh(AstrMessageEvent("/dsh 跑一下")))
    check("mid-turn error is reported", "会话中途报错，原因：429 Rate Limit Reached" in failed[0], failed[0])

    files = main.DshBridgePlugin(FakeContext(), {"progress_message": False})
    files._client = FakeClient()
    files._client.attachments = [
        {"index": 1, "path": "D:/out/report.md", "name": "report.md"},
        {"index": 2, "path": "D:/out/chart.png", "name": "chart.png"},
    ]
    with_files = await collect(files.dsh(AstrMessageEvent("/dsh 出报告")))
    check("attachments are listed at the end", with_files[0].rstrip().endswith("附件1：D:/out/report.md\n附件2：D:/out/chart.png"), with_files[0])

    check("duration formatting", main._duration(3900) == "1 时 5 分" and main._duration(120) == "2 分", main._duration(3900))

    pusher = main.DshBridgePlugin(ctx, {})
    await pusher._push("qq/group/1", {
        "kind": "result",
        "title": "修复登录",
        "text": "搞定了",
        "attachments": [{"path": "D:/out/a.log"}],
    })
    check("proactive message is pushed", ctx.sent and ctx.sent[-1][0] == "qq/group/1", ctx.sent[-1] if ctx.sent else None)
    pushed = ctx.sent[-1][1].text if ctx.sent else ""
    check("proactive message carries title and body", "修复登录" in pushed and "搞定了" in pushed, pushed)
    check("proactive message lists attachments", "附件1：D:/out/a.log" in pushed, pushed)

    # Long answers are split on line boundaries.
    long_text = "\n".join(f"line {index}" for index in range(400))
    chunks = main._split(long_text, 500)
    check("long answers split", len(chunks) > 1 and all(len(chunk) <= 520 for chunk in chunks), len(chunks))

    return failures


if __name__ == "__main__":
    failures = asyncio.run(main_checks())
    print("plugin-logic:", "PASS" if failures == 0 else f"{failures} FAILURE(S)")
    raise SystemExit(0 if failures == 0 else 1)
