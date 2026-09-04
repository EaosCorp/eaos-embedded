"""Minimal MQTT 3.1.1 client — the QoS-0 subset, stdlib asyncio only.

Just enough MQTT to talk to a broker on behalf of the shim and the bench
tools: CONNECT/CONNACK, SUBSCRIBE/SUBACK, PUBLISH both directions at QoS 0,
PINGREQ/PINGRESP. No QoS 1/2, wills, retained publishing, or auth — the
legacy gateway's own traffic is all QoS 0 anonymous, and the embedded
family installs with nothing beyond the standard library.

Reconnects with backoff and re-subscribes. QoS-0 publishes while
disconnected return False (callers that care check the return).
"""
from __future__ import annotations

import asyncio
import json


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


class MQTTClient:
    """on_message(topic: str, payload: bytes) is called from the reader task."""

    def __init__(self, host="127.0.0.1", port=1883, client_id="uii-mqtt",
                 on_message=None, keepalive=30):
        self.host, self.port = host, int(port)
        self.client_id = client_id
        self.on_message = on_message
        self.keepalive = keepalive
        self.connected = False
        self._subs: list[str] = []
        self._writer = None
        self._pkid = 0

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
        self._writer.write(data)
        await self._writer.drain()

    async def _subscribe_now(self, topic_filter: str):
        self._pkid = self._pkid % 0xFFFF + 1
        body = self._pkid.to_bytes(2, "big") + _mstr(topic_filter) + b"\x00"
        await self._send(_packet(0x82, body))

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
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
                self._writer = writer
                connect = (_mstr("MQTT") + b"\x04\x02"
                           + self.keepalive.to_bytes(2, "big")
                           + _mstr(self.client_id))
                await self._send(_packet(0x10, connect))
                head, body = await self._read_packet(reader)
                if head >> 4 != 2 or len(body) < 2 or body[1] != 0:
                    raise ConnectionError(f"CONNACK refused: {body!r}")
                self.connected = True
                backoff = 1.0
                for t in list(self._subs):
                    await self._subscribe_now(t)
                ping = asyncio.ensure_future(self._ping_loop())
                try:
                    while True:
                        head, body = await self._read_packet(reader)
                        if head >> 4 == 3:      # PUBLISH (QoS 0: no packet id)
                            tlen = int.from_bytes(body[:2], "big")
                            topic = body[2:2 + tlen].decode()
                            if self.on_message:
                                self.on_message(topic, body[2 + tlen:])
                        # SUBACK / PINGRESP need no action
                finally:
                    ping.cancel()
            except (OSError, ConnectionError, asyncio.IncompleteReadError):
                pass
            self.connected = False
            self._writer = None
            await asyncio.sleep(backoff)
            backoff = min(15.0, backoff * 1.7)

    async def _ping_loop(self):
        while True:
            await asyncio.sleep(self.keepalive * 0.6)
            try:
                await self._send(b"\xc0\x00")
            except Exception:
                return
