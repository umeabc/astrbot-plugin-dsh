"""Client for dsh-astrbot-bridge.

Two transports carry the same small JSON API:

* ``direct`` — plain HTTP to the bridge on this machine or the LAN.
* ``relay`` — the bridge keeps an outbound connection to a rendezvous server;
  this side joins the same room and speaks HTTP inside an AES-128-GCM tunnel,
  so neither machine needs a reachable port.

Only the relay path needs ``cryptography`` (which AstrBot already depends on).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
from dataclasses import dataclass
from typing import Any

FRAME_HELLO = 0
FRAME_OPEN = 1
FRAME_DATA = 2
FRAME_FIN = 3
FRAME_RST = 4
FRAME_PING = 5
FRAME_CHANNEL_CLOSED = 6

MAX_CHUNK = 16 * 1024
HEADER = 8
MAX_RESPONSE = 8 * 1024 * 1024


class BridgeError(Exception):
    """A failed bridge call: transport, protocol, or an API error response."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass
class BridgeResponse:
    status: int
    data: dict[str, Any]
    raw: bytes

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.data.get("ok") is True


# --------------------------------------------------------------------------- #
# HTTP over a byte stream
# --------------------------------------------------------------------------- #

def build_http_request(
    method: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> bytes:
    body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [
        f"{method.upper()} {path} HTTP/1.1",
        "Host: 127.0.0.1",
        "User-Agent: astrbot-plugin-dsh",
        "Accept: application/json",
        "Connection: close",
        f"Authorization: Bearer {token}",
    ]
    if body:
        headers.append("Content-Type: application/json; charset=utf-8")
        headers.append(f"Content-Length: {len(body)}")
    head = "\r\n".join(headers).encode("utf-8") + b"\r\n\r\n"
    return head + body


def _decode_chunked(body: bytes) -> bytes:
    out = bytearray()
    index = 0
    while index < len(body):
        end = body.find(b"\r\n", index)
        if end < 0:
            break
        size_line = body[index:end].split(b";")[0].strip()
        if not size_line:
            index = end + 2
            continue
        try:
            size = int(size_line, 16)
        except ValueError:
            break
        index = end + 2
        if size == 0:
            break
        out += body[index : index + size]
        index += size + 2
    return bytes(out)


def parse_http_response(raw: bytes) -> BridgeResponse:
    split = raw.find(b"\r\n\r\n")
    if split < 0:
        raise BridgeError(f"malformed HTTP response from the bridge ({len(raw)} bytes)")
    head = raw[:split].decode("iso-8859-1")
    body = raw[split + 4 :]
    lines = head.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2 or not parts[1].isdigit():
        raise BridgeError(f"malformed HTTP status line: {lines[0]!r}")
    status = int(parts[1])
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name:
            headers[name.strip().lower()] = value.strip()
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = _decode_chunked(body)
    elif "content-length" in headers:
        try:
            body = body[: int(headers["content-length"])]
        except ValueError:
            pass
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BridgeError(f"bridge answered with invalid JSON: {error}") from error
    if not isinstance(data, dict):
        data = {"ok": False, "error": "bad-response", "detail": str(data)}
    return BridgeResponse(status=status, data=data, raw=body)


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #

class _ConnectFailure(Exception):
    """The bridge could not be reached; trying again may work."""


class DirectTransport:
    """Plain HTTP; the bridge listens on loopback (or a LAN address)."""

    ATTEMPTS = 5

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    async def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 60.0,
        retry: bool = True,
    ) -> BridgeResponse:
        url = f"{self.base_url}{path}"
        last: Exception | None = None
        attempts = self.ATTEMPTS if retry else 1
        for attempt in range(attempts):
            try:
                return await self._request_once(method, url, payload, timeout)
            except _ConnectFailure as error:
                last = error
                await asyncio.sleep(_backoff(attempt))
        raise BridgeError(f"连接失败（已重试 {attempts} 次）：{last}")

    async def _request_once(self, method, url, payload, timeout) -> BridgeResponse:
        try:
            import aiohttp
        except ImportError:  # pragma: no cover - AstrBot always ships aiohttp
            return await asyncio.to_thread(self._request_sync, method, url, payload, timeout)

        headers = {"Authorization": f"Bearer {self.token}"}
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            try:
                async with session.request(method, url, json=payload, headers=headers) as response:
                    body = await response.read()
                    status = response.status
            except aiohttp.ClientConnectorError as error:
                raise _ConnectFailure(str(error)) from error
            except aiohttp.ClientError as error:
                raise BridgeError(f"cannot reach the DSH bridge at {url}: {error}") from error
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BridgeError(f"bridge answered with invalid JSON: {error}") from error
        if not isinstance(data, dict):
            data = {"ok": False, "error": "bad-response", "detail": str(data)}
        return BridgeResponse(status=status, data=data, raw=body)

    def _request_sync(self, method, url, payload, timeout):  # pragma: no cover - fallback
        import urllib.error
        import urllib.request

        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method=method.upper(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
                return parse_http_response(
                    f"HTTP/1.1 {response.status} X\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data
                )
        except urllib.error.HTTPError as error:
            # A 4xx from the bridge is a protocol answer, not a transport fault.
            data = error.read()
            return parse_http_response(
                f"HTTP/1.1 {error.code} X\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data
            )
        except (urllib.error.URLError, OSError) as error:
            raise _ConnectFailure(f"cannot reach the DSH bridge at {url}: {error}") from error


class RelayTransport:
    """HTTP carried inside the bridge's relay room, sealed with AES-128-GCM.

    A connect/handshake failure is retried silently (five attempts, growing
    backoff); once the request itself has been written, a broken stream is
    reported instead of replayed, because replaying could run the task twice.
    """

    ATTEMPTS = 5

    def __init__(self, relay_url: str, room: str, key: str, token: str) -> None:
        self.host, self.port = parse_relay_address(relay_url)
        self.room = room.strip().lower()
        self.key = _decode_room_key(key)
        self.token = token

    async def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 60.0,
        retry: bool = True,
    ) -> BridgeResponse:
        request_bytes = build_http_request(method, path, self.token, payload)
        last: Exception | None = None
        attempts = self.ATTEMPTS if retry else 1
        for attempt in range(attempts):
            try:
                reader, writer, channel, tx, rx = await self._open()
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, _RelayRefused, _RelayError) as error:
                last = error
                await asyncio.sleep(_backoff(attempt))
                continue

            stream = int.from_bytes(os.urandom(4), "big") & 0x7FFFFFFF or 1
            try:
                raw = await asyncio.wait_for(
                    self._roundtrip(reader, writer, channel, tx, rx, stream, request_bytes),
                    timeout=timeout,
                )
                return parse_http_response(raw)
            except asyncio.TimeoutError:
                raise BridgeError("DSH 没有在超时时间内回复") from None
            except (OSError, asyncio.IncompleteReadError, _RelayError) as error:
                raise BridgeError(f"relay 连接中断：{error}") from error
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001 - best effort
                    pass
        raise BridgeError(f"连接失败（已重试 {attempts} 次）：{last}")

    async def _open(self):
        """Connect and complete the room handshake; safe to retry."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        salt = os.urandom(16)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=15,
        )
        try:
            handshake = json.dumps(
                {
                    "v": 1,
                    "role": "client",
                    "room": self.room,
                    "salt": base64.b64encode(salt).decode("ascii"),
                }
            ).encode("utf-8")
            writer.write(struct.pack(">I", len(handshake)) + handshake)
            await writer.drain()

            length = struct.unpack(">I", await reader.readexactly(4))[0]
            if length == 0 or length > 4096:
                raise _RelayError("bad relay reply")
            reply = json.loads((await reader.readexactly(length)).decode("utf-8"))
            if reply.get("ok") is not True:
                raise _RelayRefused(f"relay 拒绝加入房间：{reply.get('error', 'unknown')}")
            channel = int(reply.get("channel", 0)) & 0xFF

            def derive(info: bytes) -> bytes:
                return HKDF(
                    algorithm=hashes.SHA256(), length=16, salt=salt, info=info
                ).derive(self.key)

            return (
                reader,
                writer,
                channel,
                AESGCM(derive(b"dshm/c2h")),
                AESGCM(derive(b"dshm/h2c")),
            )
        except BaseException:
            writer.close()
            raise

    async def _roundtrip(self, reader, writer, channel, tx, rx, stream, request_bytes):
        writer.write(_frame(0, stream, FRAME_OPEN))
        counter = 0
        for offset in range(0, len(request_bytes), MAX_CHUNK):
            piece = request_bytes[offset : offset + MAX_CHUNK]
            header = _frame_header(0, stream, FRAME_DATA, len(piece) + 16)
            sealed = tx.encrypt(_nonce(counter), piece, header[1:])
            counter += 1
            writer.write(header + sealed)
        await writer.drain()

        received = bytearray()
        rx_counter = 0
        while True:
            header = await reader.readexactly(HEADER)
            frame_channel = header[0]
            frame_stream = struct.unpack(">I", header[1:5])[0]
            frame_type = header[5]
            body_length = struct.unpack(">H", header[6:8])[0]
            body = await reader.readexactly(body_length) if body_length else b""

            if frame_type == FRAME_DATA:
                if frame_channel != channel:
                    continue
                try:
                    received += rx.decrypt(_nonce(rx_counter), body, header[1:])
                except Exception as error:  # noqa: BLE001 - tag mismatch
                    raise _RelayError(f"channel authentication failed: {error}") from error
                rx_counter += 1
                if len(received) > MAX_RESPONSE:
                    raise _RelayError("response too large")
                continue
            if frame_type == FRAME_FIN and frame_stream == stream:
                break
            if frame_type == FRAME_RST:
                raise _RelayError("the bridge reset the stream")
            if frame_type == FRAME_CHANNEL_CLOSED:
                raise _RelayError("the relay closed this channel")
            # PING and anything else are ignored.
        return bytes(received)


class _RelayRefused(Exception):
    """The relay answered but refused the room; a retry may succeed."""


class _RelayError(Exception):
    pass


def _backoff(attempt: int) -> float:
    return (0.0, 0.5, 1.0, 2.0, 4.0)[min(attempt, 4)]


def parse_relay_address(value: str) -> tuple[str, int]:
    address = (value or "").strip()
    if address.lower().startswith("tcp://"):
        address = address[6:]
    if not address:
        raise BridgeError("relay_url is empty")
    if address.startswith("["):  # [ipv6]:port
        end = address.find("]")
        host = address[1:end]
        port = int(address[end + 2 :]) if end >= 0 and address[end + 1 : end + 2] == ":" else 9443
        return host, port
    if ":" in address:
        host, _, tail = address.rpartition(":")
        if host and tail.isdigit():
            return host, int(tail)
    return address, 9443


def _decode_room_key(key: str) -> bytes:
    raw = (key or "").strip().replace("-", "+").replace("_", "/")
    raw += "=" * (-len(raw) % 4)
    try:
        secret = base64.b64decode(raw, validate=True)
    except Exception as error:  # noqa: BLE001
        raise BridgeError(f"relay_key is not valid base64url: {error}") from error
    if len(secret) != 16:
        raise BridgeError("relay_key must decode to 16 bytes")
    return secret


def _nonce(counter: int) -> bytes:
    return b"\x00" * 4 + counter.to_bytes(8, "big")


def _frame_header(channel: int, stream: int, frame_type: int, length: int) -> bytes:
    return bytes([channel & 0xFF]) + struct.pack(">I", stream & 0xFFFFFFFF) + bytes(
        [frame_type & 0xFF]
    ) + struct.pack(">H", length & 0xFFFF)


def _frame(channel: int, stream: int, frame_type: int) -> bytes:
    return _frame_header(channel, stream, frame_type, 0)


# --------------------------------------------------------------------------- #
# Facade
# --------------------------------------------------------------------------- #

class BridgeClient:
    """Thin, transport-agnostic wrapper around the bridge API."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config or {}
        self.token = str(self.config.get("token", "")).strip()
        transport = str(self.config.get("transport", "direct")).strip().lower()
        if transport == "relay":
            self.transport: DirectTransport | RelayTransport = RelayTransport(
                str(self.config.get("relay_url", "")),
                str(self.config.get("relay_room", "")),
                str(self.config.get("relay_key", "")),
                self.token,
            )
        else:
            self.transport = DirectTransport(str(self.config.get("api_url", "http://127.0.0.1:8399")), self.token)
        self.timeout = max(30, int(self.config.get("timeout_seconds", 900)))

    async def health(self) -> dict[str, Any]:
        return (await self._request("GET", "/v1/health", timeout=20)).data

    async def sessions(
        self,
        chat_key: str,
        page: int | None = None,
        per_page: int | None = None,
    ) -> dict[str, Any]:
        query = f"/v1/sessions?chatKey={_quote(chat_key)}"
        if page is not None:
            query += f"&page={int(page)}"
        if per_page is not None:
            query += f"&perPage={int(per_page)}"
        return (await self._request("GET", query, timeout=20)).data

    async def chat(
        self,
        chat_key: str,
        text: str,
        *,
        follow: bool = True,
        new_session: bool = False,
        session_id: str = "",
        timeout: float | None = None,
        user: str = "",
    ) -> dict[str, Any]:
        payload = {
            "chatKey": chat_key,
            "text": text,
            "follow": follow,
            "newSession": new_session,
        }
        if session_id:
            payload["sessionId"] = session_id
        if user:
            payload["user"] = user
        response = await self._request(
            "POST", "/v1/chat", payload, timeout=timeout or self.timeout + 30,
        )
        return response.data

    async def switch(
        self,
        chat_key: str,
        session_id: str = "",
        index: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chatKey": chat_key}
        if session_id:
            payload["sessionId"] = session_id
        if index is not None:
            payload["index"] = int(index)
        response = await self._request("POST", "/v1/switch", payload, timeout=20)
        return response.data

    async def cancel(self, chat_key: str) -> dict[str, Any]:
        response = await self._request("POST", "/v1/cancel", {"chatKey": chat_key}, timeout=20)
        return response.data

    async def notifications(self, chat_key: str | None = None) -> list[dict[str, Any]]:
        query = "/v1/notify" if not chat_key else f"/v1/notify?chatKey={_quote(chat_key)}"
        data = (await self._request("GET", query, timeout=20, retry=False)).data
        items = data.get("notifications")
        return items if isinstance(items, list) else []

    async def ack(self, chat_key: str, turn_id: str) -> dict[str, Any]:
        return (
            await self._request(
                "POST", "/v1/ack", {"chatKey": chat_key, "turnId": turn_id}, timeout=20
            )
        ).data

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 60,
        retry: bool = True,
    ) -> BridgeResponse:
        if not self.token:
            raise BridgeError("token is not configured; copy it from the DSH bridge log or ~/.dsh/astrbot-bridge.token")
        try:
            response = await self.transport.request(method, path, payload, timeout=timeout, retry=retry)
        except asyncio.TimeoutError as error:
            raise BridgeError("DSH 没有在超时时间内回复") from error
        if not response.ok:
            detail = response.data.get("detail") or response.data.get("error") or f"HTTP {response.status}"
            raise BridgeError(str(detail), status=response.status, code=str(response.data.get("error", "")))
        return response


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


# --------------------------------------------------------------------------- #
# Manual probe: python dsh_astrbot_client.py --transport relay ... /v1/health
# --------------------------------------------------------------------------- #

async def _main() -> int:  # pragma: no cover - operator convenience
    import argparse

    parser = argparse.ArgumentParser(description="Probe a dsh-astrbot-bridge endpoint")
    parser.add_argument("--transport", default="direct", choices=["direct", "relay"])
    parser.add_argument("--api-url", default="http://127.0.0.1:8399")
    parser.add_argument("--relay-url", default="")
    parser.add_argument("--room", default="")
    parser.add_argument("--key", default="")
    parser.add_argument("--token", default=os.environ.get("DSH_BRIDGE_TOKEN", ""))
    parser.add_argument("path", nargs="?", default="/v1/health")
    args = parser.parse_args()

    client = BridgeClient(
        {
            "transport": args.transport,
            "api_url": args.api_url,
            "relay_url": args.relay_url,
            "relay_room": args.room,
            "relay_key": args.key,
            "token": args.token,
        }
    )
    started = time.time()
    response = await client._request("GET", args.path)
    print(f"{response.status} in {(time.time() - started) * 1000:.0f} ms")
    print(json.dumps(response.data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(_main()))
