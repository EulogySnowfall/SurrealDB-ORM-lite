"""A TCP proxy the tests can cut, to simulate a dropped WebSocket without touching the server.

Restarting the SurrealDB container would drop the connection too, but it takes seconds, loses
the data the test wrote, and would break every other test sharing that server. The proxy sits
between the ORM and the server on a random local port instead:

* :meth:`cut` aborts every connection it carries — the client sees close code 1006, exactly as
  for a network failure (measured on 2.7.0 and 3.3.2: the SDK's receive task ends within 2 ms);
* :meth:`refuse` makes new connections fail at once, as an unreachable server does, until
  :meth:`accept` lets them through again.

It runs on the test's own event loop, so it must be started and closed inside the test body.
"""

from __future__ import annotations

import asyncio
import contextlib
import os


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
        self._writers.update((writer, up_writer))
        try:
            await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer))
        finally:
            self._writers.discard(writer)
            self._writers.discard(up_writer)

    @staticmethod
    async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        try:
            while data := await source.read(65536):
                sink.write(data)
                await sink.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            # Either side ending ends the pair: a half-open proxied socket would let the client
            # believe the connection is still up.
            sink.transport.abort()
