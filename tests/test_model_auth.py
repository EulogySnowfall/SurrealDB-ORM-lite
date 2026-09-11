"""Model-level authentication — AuthenticatedUserMixin (v0.17.0)."""

import os
from typing import Any

import pytest

from surreal_orm_lite import SurrealDBConnectionManager
from surreal_orm_lite._sdk import SurrealError

HOST = os.getenv("SURREALDB_HOST", "localhost")
PORT = os.getenv("SURREALDB_PORT", "8000")


def _connect(scheme: str = "ws") -> None:
    suffix = "/rpc" if scheme in ("ws", "wss") else ""
    SurrealDBConnectionManager.set_connection(f"{scheme}://{HOST}:{PORT}{suffix}", "root", "root", "ns", "db")


class TestEphemeralClient:
    @pytest.mark.asyncio
    async def test_raises_when_no_connection_configured(self) -> None:
        await SurrealDBConnectionManager.unset_connection()
        with pytest.raises(ValueError, match="Connection not been set"):
            async with SurrealDBConnectionManager.ephemeral_client():
                pass  # pragma: no cover

    @pytest.mark.asyncio
    async def test_yields_a_connected_but_anonymous_client(self) -> None:
        """Connected enough to talk, but carrying no identity of its own.

        Both DB lines answer an anonymous ``info()`` with an IAM permissions error rather than
        a quiet ``None`` — proof the client never inherited the configured root credentials,
        which is exactly what makes it safe to authenticate as somebody else.
        """
        _connect()
        try:
            async with SurrealDBConnectionManager.ephemeral_client() as client:
                with pytest.raises(SurrealError, match="(?i)permission"):
                    await client.info()
        finally:
            await SurrealDBConnectionManager.unset_connection()

    @pytest.mark.asyncio
    async def test_does_not_enter_the_shared_per_loop_cache(self) -> None:
        """The whole point: an ephemeral client must not become *the* client."""
        _connect()
        try:
            shared = await SurrealDBConnectionManager.get_client()
            async with SurrealDBConnectionManager.ephemeral_client() as ephemeral:
                assert ephemeral is not shared
            # The shared client is untouched and still root.
            assert await SurrealDBConnectionManager.get_client() is shared
            await shared.query("INFO FOR DB;", {})
        finally:
            await SurrealDBConnectionManager.unset_connection()

    @pytest.mark.asyncio
    async def test_closes_the_client_even_when_the_body_raises(self) -> None:
        _connect()
        captured: list[Any] = []
        try:
            with pytest.raises(RuntimeError, match="boom"):
                async with SurrealDBConnectionManager.ephemeral_client() as client:
                    captured.append(client)
                    raise RuntimeError("boom")
            # A closed client refuses further use.
            with pytest.raises(SurrealError):
                await captured[0].info()
        finally:
            await SurrealDBConnectionManager.unset_connection()
