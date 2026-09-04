"""Minimal MQTT 3.1.1 client — the QoS-0 subset, stdlib asyncio only.

Just enough MQTT to talk to a broker on behalf of the shim and the bench
tools: CONNECT/CONNACK, SUBSCRIBE/SUBACK, PUBLISH both directions at QoS 0,
PINGREQ/PINGRESP. Inbound QoS 1 is acknowledged (PUBACK) and QoS 2 is
parsed but not acknowledged, so a chattier gateway cannot corrupt payloads;
outbound is QoS 0 only. No wills, retained publishing, or auth — the legacy
gateway's own traffic is all QoS 0 anonymous, and the embedded family
installs with nothing beyond the standard library.

Reconnects with backoff and re-subscribes. QoS-0 publishes while
disconnected return False (callers that care check the return).

Liveness: the broker must answer PINGREQ (sent every 0.6 × keepalive), so
no traffic at all for 1.5 × keepalive means the TCP connection is half-open
(unit lost power, cable pulled) and the client drops it and reconnects.
Without that a dead peer keeps the client hanging until the kernel gives up
on retransmits, which in the field is many minutes.

Logging is one line per state change: connected / lost / unreachable
(rate-limited while the broker stays down).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Callable, Optional

CONNECT_TIMEOUT_S = 10.0
MAX_BACKOFF_S = 15.0
UNREACHABLE_LOG_EVERY_S = 60.0


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _mstr(s: str) -> bytes:
    b = s.encode()
    return len(b).to_bytes(2, "big") + b


def _packet(header: int, body: bytes) -> bytes:
    return bytes([header]) + _varint(len(body)) + body


def _describe(exc: Optional[BaseException]) -> str:
    if exc is None:
        return "closed by peer"
    if isinstance(exc, asyncio.IncompleteReadError):
        return "closed by peer"
    text = str(exc).strip()
    return f"{type(exc).__name__}{': ' + text if text else ''}"


def _log(msg: str) -> None:
    print(msg, flush=True)


class MQTTClient:
    """on_message(topic: str, payload: bytes) is called from the reader task.
    on_connect() / on_disconnect(reason: str) are optional state hooks."""

    def __init__(self, host="127.0.0.1", port=1883, client_id="uii-mqtt",
                 on_message: Optional[Callable[[str, bytes], None]] = None,
                 keepalive: float = 30,
                 on_connect: Optional[Callable[[], None]] = None,
                 on_disconnect: Optional[Callable[[str], None]] = None,
                 log: Callable[[str], None] = _log):
        self.host, self.port = host, int(port)
        self.client_id = client_id
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.keepalive = float(keepalive)
        self.read_timeout = self.keepalive * 1.5
        self.log = log
        self.connected = False
        self._subs: list[str] = []
        self._writer = None
        self._pkid = 0
        self._last_unreachable_log = 0.0

    @property
    def addr(self) -> str:
        return f"{self.host}:{self.port}"

    # -- wire ---------------------------------------------------------------

    async def _read_packet(self, reader):
        head = (await reader.readexactly(1))[0]
        mult, length = 1, 0
        while True:
            b = (await reader.readexactly(1))[0]
            length += (b & 0x7F) * mult
            if not (b & 0x80):
                break
            mult *= 128
        body = await reader.readexactly(length) if length else b""
        return head, body

    async def _send(self, data: bytes):
        if self._writer is None:
            raise ConnectionError("not connected")
        self._writer.write(data)
        await self._writer.drain()

    async def _subscribe_now(self, topic_filter: str):
        self._pkid = self._pkid % 0xFFFF + 1
        body = self._pkid.to_bytes(2, "big") + _mstr(topic_filter) + b"\x00"
        await self._send(_packet(0x82, body))

    # -- api ----------------------------------------------------------------

    async def subscribe(self, topic_filter: str):
        if topic_filter not in self._subs:
            self._subs.append(topic_filter)
        if self.connected:
            await self._subscribe_now(topic_filter)

    async def publish(self, topic: str, payload) -> bool:
        if not self.connected:
            return False
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        if isinstance(payload, str):
            payload = payload.encode()
        try:
            await self._send(_packet(0x30, _mstr(topic) + payload))
            return True
        except (ConnectionResetError, BrokenPipeError, OSError):
            self.connected = False
            return False

    async def run(self):
        """Maintain the connection forever; run as a task."""
        backoff = 1.0
        while True:
            reason: Optional[BaseException] = None
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=CONNECT_TIMEOUT_S)
                self._writer = writer
                connect = (_mstr("MQTT") + b"\x04\x02"
                           + int(self.keepalive).to_bytes(2, "big")
                           + _mstr(self.client_id))
                await self._send(_packet(0x10, connect))
                head, body = await asyncio.wait_for(self._read_packet(reader),
                                                    timeout=CONNECT_TIMEOUT_S)
                if head >> 4 != 2 or len(body) < 2 or body[1] != 0:
                    raise ConnectionError(f"CONNACK refused: {body!r}")
                self.connected = True
                backoff = 1.0
                self.log(f"[mqtt] connected {self.addr} as {self.client_id}")
                for t in list(self._subs):
                    await self._subscribe_now(t)
                if self.on_connect:
                    self.on_connect()
                ping = asyncio.ensure_future(self._ping_loop())
                try:
                    await self._reader_loop(reader)
                finally:
                    ping.cancel()
            except (OSError, ConnectionError, asyncio.IncompleteReadError,
                    asyncio.TimeoutError) as e:
                reason = e
            except asyncio.CancelledError:
                self._close()                 # task cancelled: leave no socket behind
                raise
            was_connected = self.connected
            self._close()
            self._note_down(was_connected, reason)
            await asyncio.sleep(backoff)
            backoff = min(MAX_BACKOFF_S, backoff * 1.7)

    async def _reader_loop(self, reader):
        while True:
            try:
                head, body = await asyncio.wait_for(self._read_packet(reader),
                                                    timeout=self.read_timeout)
            except asyncio.TimeoutError:
                raise ConnectionError(
                    f"no traffic for {self.read_timeout:.0f}s "
                    f"(half-open socket?)") from None
            if head >> 4 != 3:          # SUBACK / PINGRESP / PUBACK: no action
                continue
            qos = (head >> 1) & 0x03
            tlen = int.from_bytes(body[:2], "big")
            topic = body[2:2 + tlen].decode()
            rest = body[2 + tlen:]
            if qos:
                pkid, rest = rest[:2], rest[2:]
                if qos == 1:
                    await self._send(_packet(0x40, pkid))
            if self.on_message:
                self.on_message(topic, rest)

    def _close(self):
        self.connected = False
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None

    def _note_down(self, was_connected: bool, reason):
        text = _describe(reason)
        if was_connected:
            self.log(f"[mqtt] lost {self.addr}: {text}; reconnecting")
            if self.on_disconnect:
                self.on_disconnect(text)
            self._last_unreachable_log = time.time()
        elif time.time() - self._last_unreachable_log >= UNREACHABLE_LOG_EVERY_S:
            self.log(f"[mqtt] {self.addr} unreachable: {text}; retrying")
            self._last_unreachable_log = time.time()

    async def _ping_loop(self):
        while True:
            await asyncio.sleep(self.keepalive * 0.6)
            try:
                await self._send(b"\xc0\x00")
            except Exception:
                return
