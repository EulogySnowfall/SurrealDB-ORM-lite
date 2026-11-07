"""A TCP proxy the tests can cut, to simulate a dropped WebSocket without touching the server.

Restarting the SurrealDB container would drop the connection too, but it takes seconds, loses
the data the test wrote, and would break every other test sharing that server. The proxy sits
between the ORM and the server on a random local port instead:

* :meth:`cut` aborts every connection it carries — the client sees close code 1006, exactly as
  for a network failure (measured on 2.7.0, 3.3.2 and 3.3.3: the SDK's receive task ends within 2 ms);
* :meth:`refuse` makes new connections fail at once, as an unreachable server does, until
  :meth:`accept` lets them through again;
* :attr:`log` (v0.50.0) holds, per connection it carried, the RPC requests the client sent — method and parameters,
  read off the WebSocket frames — so a test can see under which identity each connection worked.

It runs on the test's own event loop, so it must be started and closed inside the test body.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import zlib
from typing import Any

from surreal_orm_lite._sdk import cbor_decode


class _RequestLog:
    """The RPC requests of one client→server WebSocket stream: ``(method, params)`` in the order they were sent.

    Fed the raw bytes the client wrote: the HTTP upgrade request first, then client frames (always masked, possibly
    fragmented, possibly ``permessage-deflate`` compressed — RSV1 on the first frame). Each complete data message is
    decoded as CBOR; a message that does not decode is logged as ``("?", None)``. Control frames are skipped.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, Any]] = []
        self._buffer = b""
        self._upgraded = False
        self._message = b""
        self._compressed = False
        self._inflater = zlib.decompressobj(-zlib.MAX_WBITS)

    def methods(self) -> list[str]:
        return [method for method, _ in self.requests]

    def feed(self, data: bytes) -> None:
        self._buffer += data
        if not self._upgraded:
            _head, sep, rest = self._buffer.partition(b"\r\n\r\n")
            if not sep:
                return
            self._upgraded, self._buffer = True, rest
        while (frame := self._next_frame()) is not None:
            self._on_frame(*frame)

    def _next_frame(self) -> tuple[int, int, bytes] | None:
        buf = self._buffer
        if len(buf) < 2:
            return None
        first, second = buf[0], buf[1]
        length, offset = second & 0x7F, 2
        if length == 126:
            if len(buf) < 4:
                return None
            length, offset = int.from_bytes(buf[2:4], "big"), 4
        elif length == 127:
            if len(buf) < 10:
                return None
            length, offset = int.from_bytes(buf[2:10], "big"), 10
        mask = b""
        if second & 0x80:
            mask, offset = buf[offset : offset + 4], offset + 4
        if len(buf) < offset + length:
            return None
        payload = buf[offset : offset + length]
        self._buffer = buf[offset + length :]
        if mask:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return first, first & 0x0F, payload

    def _on_frame(self, first: int, opcode: int, payload: bytes) -> None:
        if opcode >= 0x8:  # close, ping, pong
            return
        if opcode != 0x0:  # the first frame of a message
            self._message, self._compressed = b"", bool(first & 0x40)
        self._message += payload
        if not first & 0x80:  # FIN not set: more fragments follow
            return
        message = self._message
        try:
            if self._compressed:
                message = self._inflater.decompress(message + b"\x00\x00\xff\xff")
            request = cbor_decode(message)
            self.requests.append((str(request["method"]), request.get("params")))
        except Exception:
            self.requests.append(("?", None))


class CuttableProxy:
    """Forward ``127.0.0.1:<port>`` to the test SurrealDB, with a kill switch."""

    def __init__(self, target_host: str | None = None, target_port: int | None = None) -> None:
        self._target_host = target_host or os.environ.get("SURREALDB_HOST", "localhost")
        self._target_port = target_port or int(os.environ.get("SURREALDB_PORT", "8000"))
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._refusing = False
        self.port = 0
        #: Connections accepted so far — each one is a client the ORM opened.
        self.connections = 0
        #: One entry per connection accepted, in order: the RPC requests its client sent (:class:`_RequestLog`).
        self.log: list[_RequestLog] = []

    def url(self, scheme: str = "ws") -> str:
        suffix = "/rpc" if scheme.startswith("ws") else ""
        return f"{scheme}://127.0.0.1:{self.port}{suffix}"

    async def start(self) -> CuttableProxy:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    def cut(self) -> None:
        """Abort every connection in flight, like a network failure (no close frame)."""
        for writer in list(self._writers):
            writer.transport.abort()
        self._writers.clear()

    def refuse(self) -> None:
        """Reject new connections until :meth:`accept`."""
        self._refusing = True

    def accept(self) -> None:
        self._refusing = False

    async def close(self) -> None:
        self.cut()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> CuttableProxy:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._refusing:
            writer.transport.abort()
            return
        try:
            up_reader, up_writer = await asyncio.open_connection(self._target_host, self._target_port)
        except OSError:
            writer.transport.abort()
            return
        self.connections += 1
        requests = _RequestLog()
        self.log.append(requests)
        self._writers.update((writer, up_writer))
        try:
            await asyncio.gather(self._pipe(reader, up_writer, requests), self._pipe(up_reader, writer))
        finally:
            self._writers.discard(writer)
            self._writers.discard(up_writer)

    @staticmethod
    async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter, requests: _RequestLog | None = None) -> None:
        try:
            while data := await source.read(65536):
                if requests is not None:
                    requests.feed(data)
                sink.write(data)
                await sink.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            # Either side ending ends the pair: a half-open proxied socket would let the client
            # believe the connection is still up.
            sink.transport.abort()
