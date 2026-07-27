import contextlib
import functools
import logging
import typing
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_core import ValidationError

from ._sdk import NotFoundError, RecordID, ServerError
from .connection_manager import SurrealDBConnectionManager
from .exceptions import SurrealDbError
from .functions import SurrealFunc, _ComputedDefault
from .signals import (
    around_delete,
    around_save,
    around_update,
    post_delete,
    post_save,
    post_update,
    pre_delete,
    pre_save,
    pre_update,
)
from .transaction import Transaction
from .utils import (
    build_set_clause,
    merge_extra_vars,
    remove_quotes_for_variables,
    validate_alias_name,
    validate_edge_name,
    validate_field_name,
    validate_graph_path,
    validate_patch_operations,
    validate_thing,
)

logger = logging.getLogger(__name__)


class SurrealConfigDict(ConfigDict):
    """
    SurrealConfigDict is a configuration dictionary for SurrealDB models.

    Attributes:
        primary_key (str | None): The primary key field name for the model.
    """

    primary_key: str | None
    " The primary key field name for the model. "


class BaseSurrealModel(BaseModel):
    """
    Base class for models interacting with SurrealDB.
    """

    __surreal_computed__: typing.ClassVar[dict[str, str]] = {}
    """Computed field name → SurrealQL expression, collected by ``__init_subclass__``."""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Collect ``Computed(...)`` defaults before Pydantic builds the model.

        Runs inside ``type.__new__``, i.e. before Pydantic's ``ModelMetaclass`` finishes
        collecting fields — so replacing each sentinel with ``None`` here is what turns the
        attribute into an ordinary nullable field with a ``None`` default. Parent expressions
        are merged first, so a subclass inherits them and may override by redeclaring.
        """
        collected: dict[str, str] = {}
        for base in reversed(cls.__mro__[1:]):
            collected.update(getattr(base, "__surreal_computed__", None) or {})
        for name, value in list(vars(cls).items()):
            if isinstance(value, _ComputedDefault):
                collected[name] = value.expression
                setattr(cls, name, None)
        cls.__surreal_computed__ = collected
        super().__init_subclass__(**kwargs)

    @classmethod
    def get_computed_fields(cls) -> dict[str, str]:
        """Return ``{field_name: SurrealQL expression}`` for this model's computed fields.

        Declaration order, inherited fields first. Returns a copy — this is the single source
        of truth every write path consults. ``{}`` for a model with no computed fields.
        """
        return dict(cls.__surreal_computed__)

    @classmethod
    def computed_field_ddl(cls, overwrite: bool = True) -> list[str]:
        """Render the ``DEFINE FIELD … VALUE`` statements for this model's computed fields.

        Pure: builds the SurrealQL and returns it without touching the database, so it can be
        printed, diffed, or fed into a migration. One statement per computed field, in
        declaration order; ``[]`` when the model has none.

        Args:
            overwrite: ``True`` (default) emits ``DEFINE FIELD OVERWRITE …`` — the model is the
                source of truth, so re-running converges the database onto the code and an
                edited expression takes effect. ``False`` emits ``DEFINE FIELD IF NOT EXISTS …``,
                which never disturbs an existing definition.

        Note:
            No ``TYPE`` clause is emitted: SurrealDB infers an optional type from the
            expression on both 2.6.x and 3.x. Explicit field types are a later roadmap item.
        """
        computed = cls.get_computed_fields()
        if not computed:
            return []
        table = cls.get_table_name()
        validate_alias_name(table)
        clause = "OVERWRITE" if overwrite else "IF NOT EXISTS"
        statements: list[str] = []
        for name, expression in computed.items():
            validate_alias_name(name)
            statements.append(f"DEFINE FIELD {clause} {name} ON {table} VALUE {expression};")
        return statements

    @classmethod
    def get_table_name(cls) -> str:
        """
        Get the table name for the model.
        """
        return cls.__name__

    @classmethod
    def get_index_primary_key(cls) -> str | None:
        """
        Get the primary key field name for the model.
        """
        if hasattr(cls, "model_config"):  # pragma: no cover
            primary_key = cls.model_config.get("primary_key", None)
            if isinstance(primary_key, str):
                return primary_key

        return None

    def get_id(self) -> None | str | RecordID:
        """
        Get the raw ID of the model instance.

        Returns the value as stored: a native ``RecordID`` for records loaded
        from the database, or the raw value for in-memory instances.
        """
        if hasattr(self, "id"):
            id_value = self.id  # type: ignore[attr-defined]
            return id_value if id_value is not None else None

        if hasattr(self, "model_config"):
            primary_key = self.model_config.get("primary_key", None)
            if isinstance(primary_key, str) and hasattr(self, primary_key):
                primary_key_value = getattr(self, primary_key)
                return primary_key_value if primary_key_value is not None else None

        return None  # pragma: no cover

    def get_raw_id(self) -> str | None:
        """Return the bare identifier portion (without the table prefix)."""
        id_value = self.get_id()
        if id_value is None:
            return None
        if isinstance(id_value, RecordID):
            return str(id_value.id)
        return str(id_value)

    def _record_id(self) -> RecordID | None:
        """Return a ``RecordID`` suitable for passing to SDK 2.0 methods."""
        id_value = self.get_id()
        if id_value is None:
            return None
        if isinstance(id_value, RecordID):
            return id_value
        return RecordID(self.get_table_name(), id_value)

    @classmethod
    def from_db(cls, record: dict | list) -> Self | list[Self]:
        """
        Create an instance from a SurrealDB record.
        """
        if isinstance(record, list):
            return [cls.from_db(rs) for rs in record]  # type: ignore

        return cls(**record)

    @model_validator(mode="before")
    @classmethod
    def set_data(cls, data: Any) -> Any:
        """
        Pre-process data before model validation.
        Extracts the ID from RecordID if present, unless the field accepts RecordID natively.
        """
        if isinstance(data, dict) and "id" in data and isinstance(data["id"], RecordID):
            # Only convert to plain string when the id field does not accept RecordID.
            id_field = cls.model_fields.get("id")
            if id_field is not None:
                annotation = id_field.annotation
                args = typing.get_args(annotation)
                field_accepts_record_id = RecordID in (args if args else (annotation,))
            else:
                field_accepts_record_id = False
            if not field_accepts_record_id:
                # Use the RecordID's raw identifier directly. Avoid str(rid).split(":")
                # which yields the SurrealQL-escaped form (e.g. "⟨1⟩") for numeric-looking
                # or special string ids.
                data["id"] = str(data["id"].id)
        return data

    async def refresh(self, tx: Transaction | None = None) -> None:
        """Refresh the model instance from the database.

        When ``tx`` is provided, the read participates in the transaction. This requires an
        ``InteractiveTransaction`` (WebSocket + SurrealDB 3.x); on a ``BufferedTransaction``
        (HTTP or SurrealDB 2.6.x) reads are not supported and this raises ``SurrealDbError``.
        """
        if tx is not None:
            if not tx.is_interactive:
                raise SurrealDbError(
                    "refresh() is a read; it requires a WebSocket connection to SurrealDB 3.x "
                    "(native interactive transactions)."
                )
            if not self.get_id():
                raise SurrealDbError("Can't refresh data, not recorded yet.")
            rows = await tx.run_read("SELECT * FROM $rid;", {"rid": self._record_id()})
            record = rows[0] if isinstance(rows, list) and rows else rows
            if not isinstance(record, dict):
                raise SurrealDbError("Can't refresh data, no record found.")
            for key, value in record.items():
                if hasattr(self, key):
                    object.__setattr__(self, key, value)
            return
        if not self.get_id():
            raise SurrealDbError("Can't refresh data, not recorded yet.")  # pragma: no cover

        client = await SurrealDBConnectionManager.get_client()
        record = await client.select(self._record_id())

        if record is None:
            raise SurrealDbError("Can't refresh data, no record found.")  # pragma: no cover

        # SDK 2.0 returns a list even for single record select
        if isinstance(record, list):
            if len(record) == 0:
                raise SurrealDbError("Can't refresh data, no record found.")  # pragma: no cover
            record = record[0]

        # Update current instance with refreshed data (keep native RecordID for id)
        if isinstance(record, dict):
            for key, value in record.items():
                if hasattr(self, key):
                    object.__setattr__(self, key, value)

    async def _do_save(self, tx: Transaction | None = None) -> tuple[Self, bool]:
        """Internal save logic. Returns (self, created).

        When ``tx`` is provided the CREATE statement is buffered (deferred to commit)
        and the in-memory instance is returned as-is. Buffered creates require an
        explicit record id.
        """
        record_id = self._record_id()
        data = self.model_dump(exclude={"id"})
        table = self.get_table_name()

        if tx is not None:
            if record_id is not None:
                await tx.add(f"CREATE {record_id} CONTENT $data;", {"data": data})
                return self, True
            if not tx.is_interactive:
                raise SurrealDbError(
                    "save(tx=...) requires an explicit id on a buffered transaction "
                    "(auto-id requires a WebSocket connection to SurrealDB 3.x)."
                )
            rows = await tx.add(f"CREATE {table} CONTENT $data;", {"data": data})
            record = rows[0] if isinstance(rows, list) and rows else rows
            if isinstance(record, dict):
                for key, value in record.items():
                    if hasattr(self, key):
                        object.__setattr__(self, key, value)
            return self, True

        client = await SurrealDBConnectionManager.get_client()

        if record_id is not None:
            # SDK 2.0 raises a structured exception instead of returning a string.
            # The "already exists" error maps to AlreadyExistsError on SurrealDB 3.x
            # but to InternalError on 2.x (no structured kind) — both subclass
            # ServerError, so catch the base and match on the message to stay
            # faithful to the original "already exists" contract across versions.
            try:
                await client.create(record_id, data)
            except ServerError as e:
                if "already exists" in str(e).lower():
                    raise SurrealDbError(f"There was a problem with the database: {e}") from e
                raise
            return self, True

        # Auto-generate the ID
        record = await client.create(table, data)  # pragma: no cover

        if isinstance(record, list):
            raise SurrealDbError("Can't save data, multiple records returned.")  # pragma: no cover

        if record is None:
            raise SurrealDbError("Can't save data, no record returned.")  # pragma: no cover

        # Update current instance with the auto-generated ID (kept as native RecordID)
        if isinstance(record, dict):
            for key, value in record.items():
                if hasattr(self, key):
                    object.__setattr__(self, key, value)
            return self, True

        raise SurrealDbError("Can't save data, no record returned.")  # pragma: no cover

    @staticmethod
    def _validate_server_values(
        server_values: Mapping[str, Any] | None,
        extra_vars: Mapping[str, Any] | None,
    ) -> None:
        """Validate the ``server_values`` / ``extra_vars`` pair before compiling a statement.

        Key syntax is checked later by :func:`build_set_clause`; what must be caught here
        is (a) a non-``SurrealFunc`` server value — once merged with the field data the
        two are indistinguishable, so it would silently be bound as a literal string
        instead of evaluated — and (b) ``extra_vars`` with nothing to reference them.
        """
        if extra_vars and not server_values:
            raise ValueError("extra_vars requires server_values: no SurrealFunc expression could reference them.")
        for key, value in (server_values or {}).items():
            if not isinstance(value, SurrealFunc):
                raise TypeError(
                    f"server_values[{key!r}] must be a SurrealFunc, got {type(value).__name__!r}. "
                    "Wrap the expression: SurrealFunc('time::now()')."
                )

    async def _do_save_server(
        self,
        tx: Transaction | None = None,
        server_values: Mapping[str, SurrealFunc] | None = None,
        extra_vars: Mapping[str, Any] | None = None,
    ) -> tuple[Self, bool]:
        """Internal save routed through ``CREATE … SET`` so server functions are evaluated.

        The native SDK ``create()`` sends data as values, so a ``SurrealFunc`` would be
        stored as its literal text. This path compiles one ``SET`` clause instead —
        functions inlined, every other value bound — which SurrealDB evaluates
        server-side. ``CONTENT $data SET …`` is not valid SurrealQL (verified on 2.6.5
        and 3.1.3), hence the full clause rather than a hybrid.
        """
        record_id = self._record_id()
        merged: dict[str, Any] = {**self.model_dump(exclude={"id"}), **(server_values or {})}
        clause, variables = build_set_clause(merged)

        if record_id is not None:
            variables["rid"] = record_id
            statement = f"CREATE $rid SET {clause};"
        else:
            if tx is not None and not tx.is_interactive:
                raise SurrealDbError(
                    "save(tx=...) requires an explicit id on a buffered transaction "
                    "(auto-id requires a WebSocket connection to SurrealDB 3.x)."
                )
            table = self.get_table_name()
            validate_alias_name(table)
            statement = f"CREATE {table} SET {clause};"

        # `rid` is reserved on both branches (it is only *bound* on the explicit-id one), so
        # extra_vars validation behaves identically either way.
        variables = merge_extra_vars(variables, extra_vars, reserved=("rid",))

        if tx is not None:
            rows = await tx.add(statement, variables)
            self._apply_record(rows)  # interactive: applies; buffered: None → stale until commit
            return self, True

        client = await SurrealDBConnectionManager.get_client()
        try:
            rows = await client.query(statement, variables)
        except ServerError as e:
            # Same normalisation as the native path: "already exists" is an ORM error.
            if "already exists" in str(e).lower():
                raise SurrealDbError(f"There was a problem with the database: {e}") from e
            raise
        if not rows:
            raise SurrealDbError("Can't save data, no record returned.")
        self._apply_record(rows)
        return self, True

    async def save(
        self,
        tx: Transaction | None = None,
        server_values: Mapping[str, SurrealFunc] | None = None,
        extra_vars: Mapping[str, Any] | None = None,
    ) -> Self:
        """
        Save the model instance to the database.

        When ``tx`` is provided, the CREATE statement is buffered onto the
        transaction instead of being executed immediately.

        Emits pre_save and post_save signals. ``around_save`` wraps the actual write,
        so in a transaction (``tx`` provided) it is NOT emitted — the write happens at
        commit, not here — consistent with update/merge/delete in tx mode. ``post_save``
        is deferred until the transaction commits successfully; if the tx rolls back,
        ``post_save`` is NOT emitted (the write never happened).

        Args:
            tx: Optional transaction the CREATE is buffered onto.
            server_values: Field name → :class:`~surreal_orm_lite.functions.SurrealFunc`,
                evaluated **by the server** (``time::now()``, ``rand::uuid::v7()``, …)
                rather than in Python. A server value overrides a model field of the same
                name. Providing it routes the write through ``CREATE … SET``.
            extra_vars: Extra query variables the expressions reference, bound (never
                interpolated) — this is how user input reaches a function safely. Requires
                ``server_values``; a key colliding with an internal binding raises.

        Example:
            >>> await player.save(server_values={"joined_at": SurrealFunc("time::now()")})
            >>> await user.save(
            ...     server_values={"pwd": SurrealFunc("crypto::argon2::generate($password)")},
            ...     extra_vars={"password": raw_password},
            ... )

        Note:
            In a **buffered** transaction (HTTP or SurrealDB 2.6.x) the server-computed
            values are unknown until commit, so the instance keeps its pre-save values for
            those fields — ``refresh()`` after commit if you need them.
        """
        self._validate_server_values(server_values, extra_vars)
        if server_values:
            do_op = functools.partial(self._do_save_server, server_values=server_values, extra_vars=extra_vars)
            return await self._save_with_signals(do_op, tx)
        return await self._save_with_signals(self._do_save, tx)

    def _apply_record(self, record: Any) -> None:
        """Copy a returned DB row's fields onto this instance (id kept as native ``RecordID``).

        Accepts the raw SDK result (a row dict, a single-element list, or ``None``) and applies
        only attributes the model actually declares. Shared by every write path that gets a row
        back (``_do_upsert``, ``patch``, the atomic helpers, the ``server_values`` paths) so the
        "apply the row the server gave back" step lives in one place. A ``None``/empty result is
        a no-op — callers that must distinguish "no row" from "nothing to apply" check first.
        """
        if isinstance(record, list):
            record = record[0] if record else None
        if isinstance(record, dict):
            for key, value in record.items():
                if hasattr(self, key):
                    object.__setattr__(self, key, value)

    async def _run_update_returning_row(self, statement: str, variables: dict[str, Any]) -> None:
        """Run an ``UPDATE`` and apply its row, raising a uniform error when it matched nothing.

        Two server behaviours are normalised here, so ``merge(server_values=)`` fails the same
        way as the native ``merge()`` path (whose follow-up ``refresh()`` raises) on both DB
        lines:

        - a missing **record** returns no rows on 2.6.x and 3.x alike;
        - a missing **table** returns no rows on 2.6.x but raises ``NotFoundError`` on 3.x
          (same divergence the read paths normalise since v0.7.0).
        """
        client = await SurrealDBConnectionManager.get_client()
        try:
            rows = await client.query(statement, variables)
        except NotFoundError as e:
            raise SurrealDbError("Can't merge data, no record found.") from e
        if not rows:
            raise SurrealDbError("Can't merge data, no record found.")
        self._apply_record(rows)

    async def _save_with_signals(
        self,
        do_op: Callable[..., Awaitable[tuple[Self, bool]]],
        tx: Transaction | None,
    ) -> Self:
        """Shared ``save``/``upsert`` signal envelope around the concrete write ``do_op``.

        Emits ``pre_save`` → (``around_save`` wrap) → ``post_save``. In a transaction the write
        is buffered, so ``around_save`` is skipped and ``post_save`` is deferred to a
        successful commit — identical for both ``save()`` (``do_op=_do_save``) and ``upsert()``
        (``do_op=_do_upsert``), so the contract can't drift between them.
        """
        sender = self.__class__
        has_signals = pre_save.has_handlers(sender) or post_save.has_handlers(sender) or around_save.has_handlers(sender)

        if not has_signals:
            result, _created = await do_op(tx=tx)
            return result

        await pre_save.send(sender, instance=self)

        if tx is not None:
            result, created = await do_op(tx=tx)
            tx.enqueue_post_commit(lambda: post_save.send(sender, instance=self, created=created))
            return result

        async with around_save.wrap(sender, instance=self):
            result, created = await do_op(tx=tx)

        await post_save.send(sender, instance=self, created=created)
        return result

    async def _do_upsert(self, tx: Transaction | None = None) -> tuple[Self, bool]:
        """Internal upsert logic. Returns (self, created).

        Uses the SDK's native ``upsert()`` which runs ``UPSERT $record CONTENT $data`` —
        a full REPLACE: the record is created if absent, or entirely replaced if present
        (fields omitted from the model are dropped). An explicit id is required (there is
        nothing to match without one). ``created`` cannot be told apart from the native
        call, so it is reported best-effort as ``True``; use ``update_or_create`` when the
        precise ``created`` flag matters.

        When ``tx`` is provided the ``UPSERT`` statement is buffered (deferred to commit);
        on an interactive transaction the returned row is applied to ``self``.
        """
        record_id = self._record_id()
        if record_id is None:
            raise SurrealDbError(
                "upsert() requires an explicit id (there is nothing to match without one); "
                "use save() to create a record with an auto-generated id."
            )
        data = self.model_dump(exclude={"id"})

        if tx is not None:
            rows = await tx.add(f"UPSERT {record_id} CONTENT $data;", {"data": data})
            self._apply_record(rows)
            return self, True

        client = await SurrealDBConnectionManager.get_client()
        record = await client.upsert(record_id, data)
        self._apply_record(record)
        return self, True

    async def upsert(self, tx: Transaction | None = None) -> Self:
        """
        Insert the model instance, or fully replace it if a record with the same id exists.

        Backed by the SDK's native ``upsert()`` (``UPSERT $record CONTENT $data``): this is
        REPLACE semantics, so any field omitted from the model is removed from the stored
        record. For a partial update use ``merge()`` instead. An explicit id is required.

        When ``tx`` is provided, the ``UPSERT`` is buffered onto the transaction.

        Emits the same signals as ``save()`` (``pre_save``/``around_save``/``post_save``);
        in a transaction ``around_save`` is skipped and ``post_save`` is deferred to a
        successful commit (consistent with ``save(tx=)``).
        """
        return await self._save_with_signals(self._do_upsert, tx)

    async def update(self, tx: Transaction | None = None) -> Any:
        """
        Update the model instance to the database.

        When ``tx`` is provided, the UPDATE statement is buffered onto the
        transaction instead of being executed immediately.

        Emits pre_update, post_update, and around_update signals. In tx mode,
        ``around_update`` is skipped (the write is deferred to commit) and
        ``post_update`` only fires after a successful commit.
        """
        sender = self.__class__
        data = self.model_dump(exclude={"id"})
        record_id = self._record_id()
        if record_id is None:
            raise SurrealDbError("Can't update data, no id found.")

        has_signals = pre_update.has_handlers(sender) or post_update.has_handlers(sender) or around_update.has_handlers(sender)

        if tx is not None:
            if not has_signals:
                await tx.add(f"UPDATE {record_id} CONTENT $data;", {"data": data})
                return None
            update_fields = list(data.keys())
            await pre_update.send(sender, instance=self, update_fields=update_fields)
            await tx.add(f"UPDATE {record_id} CONTENT $data;", {"data": data})
            tx.enqueue_post_commit(lambda: post_update.send(sender, instance=self, update_fields=update_fields))
            return None

        client = await SurrealDBConnectionManager.get_client()

        if not has_signals:
            return await client.update(record_id, data)

        update_fields = list(data.keys())
        await pre_update.send(sender, instance=self, update_fields=update_fields)

        async with around_update.wrap(sender, instance=self, update_fields=update_fields):
            result = await client.update(record_id, data)

        await post_update.send(sender, instance=self, update_fields=update_fields)

        return result

    async def _merge_server(
        self,
        tx: Transaction | None,
        server_values: Mapping[str, SurrealFunc],
        extra_vars: Mapping[str, Any] | None,
        data: dict[str, Any],
    ) -> Any:
        """Partial update routed through ``UPDATE … SET`` so server functions are evaluated.

        ``UPDATE … SET`` only touches the listed fields, so this keeps ``merge``'s partial
        semantics while letting SurrealDB compute values. The statement returns the updated
        row (``RETURN AFTER`` by default), which replaces the ``refresh()`` round-trip the
        native MERGE path needs — and its emptiness is how a missing record is detected,
        both here (interactive tx) and in :meth:`_run_update_returning_row` (no tx). Only a
        buffered tx cannot tell: nothing runs before commit.
        """
        sender = self.__class__
        record_id = self._record_id()
        if record_id is None:
            raise SurrealDbError(f"No Id for the data to merge: {data}")

        merged: dict[str, Any] = {**data, **server_values}
        clause, variables = build_set_clause(merged)
        variables["rid"] = record_id
        variables = merge_extra_vars(variables, extra_vars)
        statement = f"UPDATE $rid SET {clause};"
        update_fields = list(merged.keys())

        has_signals = pre_update.has_handlers(sender) or post_update.has_handlers(sender) or around_update.has_handlers(sender)

        if tx is not None:
            if has_signals:
                await pre_update.send(sender, instance=self, update_fields=update_fields)
            rows = await tx.add(statement, variables)
            if tx.is_interactive:
                # An UPDATE matching nothing is not a server error — it returns no rows. The
                # native merge(tx=) path surfaces that through refresh(), which raises, so
                # raise here too (aborting the tx) instead of silently no-opping.
                if not rows:
                    raise SurrealDbError("Can't merge data, no record found.")
                self._apply_record(rows)
            else:
                # Buffered: the server-computed values are unknown until commit, so only
                # the literal kwargs can be applied — func fields stay stale. A kwarg that
                # `server_values` overrode is skipped: writing it would leave the instance
                # holding a value that never reaches the database.
                for key, value in data.items():
                    if key not in server_values and hasattr(self, key):
                        object.__setattr__(self, key, value)
            if has_signals:
                tx.enqueue_post_commit(lambda: post_update.send(sender, instance=self, update_fields=update_fields))
            return None

        if not has_signals:
            await self._run_update_returning_row(statement, variables)
            return None

        await pre_update.send(sender, instance=self, update_fields=update_fields)
        async with around_update.wrap(sender, instance=self, update_fields=update_fields):
            await self._run_update_returning_row(statement, variables)
        await post_update.send(sender, instance=self, update_fields=update_fields)
        return None

    async def merge(
        self,
        tx: Transaction | None = None,
        server_values: Mapping[str, SurrealFunc] | None = None,
        extra_vars: Mapping[str, Any] | None = None,
        **data: Any,
    ) -> Any:
        """
        Partial update of the model instance in the database.

        When ``tx`` is provided, the UPDATE…MERGE statement is buffered onto the
        transaction instead of being executed immediately.

        Note: ``tx``, ``server_values`` and ``extra_vars`` are reserved keyword arguments
        for this method. A model field literally named like one of them cannot be merged
        by keyword; use a dict-unpacking workaround if needed (no realistic SurrealDB
        column carries those names).

        Emits pre_update, post_update, and around_update signals. In tx mode,
        ``around_update`` is skipped (the write is deferred to commit) and
        ``post_update`` only fires after a successful commit. The non-tx path calls
        ``refresh()`` to resync the instance with the server; the tx path cannot
        (reads inside a tx are not supported and the write is buffered), so
        ``data`` is applied to ``self`` directly at buffer time. A rollback will
        therefore leave the instance ahead of the database — caller must treat the
        instance as stale after a failed tx.

        Args:
            tx: Optional transaction the statement is buffered onto.
            server_values: Field name → :class:`~surreal_orm_lite.functions.SurrealFunc`,
                evaluated **by the server**. A server value overrides a ``data`` keyword
                of the same name. Providing it compiles the update as ``UPDATE … SET``
                (still a partial update: unlisted fields are untouched) and the returned
                row syncs the instance, so no extra ``refresh()`` round-trip is needed.
            extra_vars: Extra query variables the expressions reference, bound (never
                interpolated). Requires ``server_values``.

        Example:
            >>> await user.merge(plan="pro", server_values={"updated_at": SurrealFunc("time::now()")})
        """
        self._validate_server_values(server_values, extra_vars)
        if server_values:
            return await self._merge_server(tx, server_values, extra_vars, data)

        sender = self.__class__
        data_set = dict(data.items())
        record_id = self._record_id()

        if record_id is None:
            raise SurrealDbError(f"No Id for the data to merge: {data}")

        has_signals = pre_update.has_handlers(sender) or post_update.has_handlers(sender) or around_update.has_handlers(sender)

        if tx is not None:
            update_fields = list(data_set.keys())
            if has_signals:
                await pre_update.send(sender, instance=self, update_fields=update_fields)
            await tx.add(f"UPDATE {record_id} MERGE $data;", {"data": data_set})
            if tx.is_interactive:
                await self.refresh(tx=tx)
            else:
                for key, value in data_set.items():
                    if hasattr(self, key):
                        object.__setattr__(self, key, value)
            if has_signals:
                tx.enqueue_post_commit(lambda: post_update.send(sender, instance=self, update_fields=update_fields))
            return None

        client = await SurrealDBConnectionManager.get_client()

        if not has_signals:
            await client.merge(record_id, data_set)
            await self.refresh()
            return

        update_fields = list(data_set.keys())
        await pre_update.send(sender, instance=self, update_fields=update_fields)

        async with around_update.wrap(sender, instance=self, update_fields=update_fields):
            await client.merge(record_id, data_set)
            await self.refresh()

        await post_update.send(sender, instance=self, update_fields=update_fields)

    # ==================== Patch & atomic operations ====================

    async def patch(self, operations: list[dict[str, Any]], tx: Transaction | None = None) -> Self:
        """Apply a JSON Patch (RFC 6902) to this record.

        Backed by the SDK's native ``patch()`` (``UPDATE rid PATCH $data``). ``operations`` is
        a list of op dicts, e.g. ``{"op": "replace", "path": "/age", "value": 26}``. Requires
        an explicit id.

        Non-transactional and interactive-tx (3.x) calls apply the server's returned row to
        ``self``. In a buffered transaction (HTTP / SurrealDB 2.6.x) the result is unknown
        until commit, so ``self`` is left stale — treat the instance as needing ``refresh()``
        (same caveat as ``merge(tx=)``).

        Unlike ``merge``/``save`` this emits NO signals: it is a low-level atomic primitive.
        ``operations`` is validated then bound as data, never string-interpolated.
        """
        validate_patch_operations(operations)
        record_id = self._record_id()
        if record_id is None:
            raise SurrealDbError("patch() requires an explicit id (there is nothing to patch without one).")
        if tx is not None:
            rows = await tx.add("UPDATE $rid PATCH $data;", {"rid": record_id, "data": operations})
            self._apply_record(rows)  # interactive: applies; buffered: add() returns None → no-op (stale)
            return self
        client = await SurrealDBConnectionManager.get_client()
        record = await client.patch(record_id, operations)
        self._apply_record(record)
        return self

    async def _atomic_update(self, set_expr: str, variables: dict[str, Any], tx: Transaction | None) -> Self:
        """Run ``UPDATE $rid SET <set_expr>`` atomically and sync ``self`` with the result.

        Shared by the atomic helpers. ``$rid`` and every value in ``variables`` are bound
        (robust for any id type; anti-injection); only the validated ``set_expr`` is inlined.
        Non-tx and interactive-tx apply the returned row to ``self``; a buffered tx leaves
        ``self`` stale (the new value is computed server-side, unknown until commit).
        """
        record_id = self._record_id()
        if record_id is None:
            raise SurrealDbError("atomic operations require an explicit id.")
        vars_: dict[str, Any] = {"rid": record_id, **variables}
        statement = f"UPDATE $rid SET {set_expr};"
        if tx is not None:
            rows = await tx.add(statement, vars_)
            self._apply_record(rows)
            return self
        client = await SurrealDBConnectionManager.get_client()
        rows = await client.query(statement, vars_)
        self._apply_record(rows)
        return self

    async def atomic_append(self, field: str, value: Any, tx: Transaction | None = None) -> Self:
        """Atomically append ``value`` to an array ``field`` (duplicates allowed).

        Compiled to ``array::append`` — identical on SurrealDB 2.6.x and 3.x. For set
        semantics (skip if already present) use :meth:`atomic_set_add`. Emits no signals.
        """
        validate_field_name(field, "atomic field")
        return await self._atomic_update(f"{field} = array::append({field}, $value)", {"value": value}, tx)

    async def atomic_remove(self, field: str, value: Any, tx: Transaction | None = None) -> Self:
        """Atomically remove ALL occurrences of ``value`` from an array ``field``.

        Compiled to ``array::complement`` — removes every occurrence, identical on 2.6.x and
        3.x. (The ``-=`` operator is deliberately NOT used: it removes all occurrences on 3.x
        but only the first on 2.6.x.) Emits no signals.
        """
        validate_field_name(field, "atomic field")
        return await self._atomic_update(f"{field} = array::complement({field}, [$value])", {"value": value}, tx)

    async def atomic_set_add(self, field: str, value: Any, tx: Transaction | None = None) -> Self:
        """Atomically add ``value`` to an array ``field`` only if not already present.

        Compiled to ``array::add`` (set semantics) — identical on 2.6.x and 3.x. (NOT the
        ``+=`` operator, which appends duplicates on both server lines.) Emits no signals.
        """
        validate_field_name(field, "atomic field")
        return await self._atomic_update(f"{field} = array::add({field}, $value)", {"value": value}, tx)

    async def atomic_increment(self, field: str, amount: Decimal | int | float = 1, tx: Transaction | None = None) -> Self:
        """Atomically add ``amount`` (default 1) to a numeric ``field``.

        Compiled to ``{field} += $amount`` — identical on 2.6.x and 3.x. Pass a negative
        ``amount`` to decrement. ``amount`` may be a ``decimal.Decimal`` for exact arithmetic
        (the value is bound, not interpolated); adding a ``Decimal`` to an int/float field
        coerces the stored field to SurrealDB ``decimal``. Emits no signals.
        """
        validate_field_name(field, "atomic field")
        return await self._atomic_update(f"{field} += $amount", {"amount": amount}, tx)

    @staticmethod
    def _as_value_list(values: Any) -> list[Any]:
        """Coerce a ``*_many`` argument to a list, rejecting scalars (incl. str) with a clear error."""
        if isinstance(values, list):
            return values
        if isinstance(values, tuple):
            return list(values)
        raise ValueError(f"values must be a list of items, got {type(values).__name__}")

    async def atomic_append_many(self, field: str, values: list[Any], tx: Transaction | None = None) -> Self:
        """Atomically append every element of ``values`` to an array ``field`` (duplicates allowed).

        The list-valued counterpart of :meth:`atomic_append` — one round-trip instead of N.
        Compiled to ``array::concat`` (NOT ``array::append``, which would nest the list as a
        single element) — identical on 2.6.x and 3.x. An empty ``values`` is a safe no-op.
        Emits no signals.
        """
        validate_field_name(field, "atomic field")
        return await self._atomic_update(
            f"{field} = array::concat({field}, $values)", {"values": self._as_value_list(values)}, tx
        )

    async def atomic_set_add_many(self, field: str, values: list[Any], tx: Transaction | None = None) -> Self:
        """Atomically add every element of ``values`` to an array ``field``, skipping any already present.

        The list-valued counterpart of :meth:`atomic_set_add` (set semantics; duplicates in
        ``values`` are also collapsed). Compiled to ``array::add`` — identical on 2.6.x and 3.x.
        An empty ``values`` is a safe no-op. Emits no signals.
        """
        validate_field_name(field, "atomic field")
        return await self._atomic_update(f"{field} = array::add({field}, $values)", {"values": self._as_value_list(values)}, tx)

    async def atomic_remove_many(self, field: str, values: list[Any], tx: Transaction | None = None) -> Self:
        """Atomically remove ALL occurrences of every element of ``values`` from an array ``field``.

        The list-valued counterpart of :meth:`atomic_remove`. Compiled to ``array::complement``
        — identical on 2.6.x and 3.x. An empty ``values`` is a safe no-op. Emits no signals.
        """
        validate_field_name(field, "atomic field")
        return await self._atomic_update(
            f"{field} = array::complement({field}, $values)", {"values": self._as_value_list(values)}, tx
        )

    async def delete(self, tx: Transaction | None = None) -> None:
        """
        Delete the model instance from the database.

        When ``tx`` is provided, the DELETE statement is buffered onto the
        transaction instead of being executed immediately.

        Emits pre_delete, post_delete, and around_delete signals. In tx mode,
        ``around_delete`` is skipped (the write is deferred to commit) and
        ``post_delete`` only fires after a successful commit.
        """
        sender = self.__class__
        record_id = self._record_id()
        if record_id is None:
            raise SurrealDbError("Can't delete data, no id found.")

        has_signals = pre_delete.has_handlers(sender) or post_delete.has_handlers(sender) or around_delete.has_handlers(sender)

        if tx is not None:
            if not has_signals:
                await tx.add(f"DELETE {record_id};", None)
                return None
            await pre_delete.send(sender, instance=self)
            await tx.add(f"DELETE {record_id};", None)
            tx.enqueue_post_commit(lambda: post_delete.send(sender, instance=self))
            return None

        client = await SurrealDBConnectionManager.get_client()

        # Record delete() is STRICT: a missing record is an error on every server
        # version. SurrealDB 2.x returns falsy; 3.x raises NotFoundError. Both map
        # to SurrealDbError (preserves the existing "not found" contract).
        if not has_signals:
            try:
                deleted = await client.delete(record_id)
            except NotFoundError as e:
                raise SurrealDbError(f"Can't delete Record id -> '{record_id}' not found!") from e
            if not deleted:
                raise SurrealDbError(f"Can't delete Record id -> '{record_id}' not found!")
            logger.info(f"Record deleted -> {deleted}.")
            return

        await pre_delete.send(sender, instance=self)

        async with around_delete.wrap(sender, instance=self):
            try:
                deleted = await client.delete(record_id)
            except NotFoundError as e:
                raise SurrealDbError(f"Can't delete Record id -> '{record_id}' not found!") from e

            if not deleted:
                raise SurrealDbError(f"Can't delete Record id -> '{record_id}' not found!")

        await post_delete.send(sender, instance=self)

        logger.info(f"Record deleted -> {deleted}.")

    @model_validator(mode="after")
    def check_config(self) -> Self:
        """
        Check the model configuration.
        """

        if not self.get_index_primary_key() and not hasattr(self, "id"):
            raise SurrealDbError(  # pragma: no cover
                "Can't create model, the model needs either 'id' field or primary_key in 'model_config'."
            )

        return self

    # ==================== Relations & Graph ====================

    def _get_thing(self) -> str:
        """Return ``table:id`` string for this instance."""
        id_val = self.get_id()
        if id_val is None:
            raise SurrealDbError("Cannot use relations on an unsaved model (no id).")
        if isinstance(id_val, RecordID):
            # A RecordID from the DB is already typed/trusted; its string form may
            # contain SurrealQL escaping (e.g. "Table:⟨1⟩") which validate_thing
            # intentionally rejects. Skip validation for trusted RecordID values.
            return str(id_val)
        thing = f"{self.get_table_name()}:{id_val}"
        validate_thing(thing)
        return thing

    @staticmethod
    def _resolve_target_thing(target: "BaseSurrealModel | str") -> str:
        """Resolve a target to ``table:id`` string."""
        if isinstance(target, str):
            validate_thing(target)
            return target
        if isinstance(target, BaseSurrealModel):
            return target._get_thing()
        raise TypeError(f"target must be a BaseSurrealModel instance or 'table:id' string, got {type(target).__name__}")

    async def relate(
        self,
        edge: str,
        target: "BaseSurrealModel | str",
        *,
        data: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Create a graph relation from this record to the target.

        Args:
            edge: The relation/edge table name (e.g. ``"follows"``).
            target: The target model instance or ``"table:id"`` string.
            data: Optional data to store on the relation edge.

        Returns:
            The created relation record(s) as returned by the database.

        Example::

            await user.relate("follows", other_user)
            await user.relate("purchased", product, data={"quantity": 2})
        """
        validate_edge_name(edge)
        source = self._get_thing()
        target_thing = self._resolve_target_thing(target)

        if data:
            set_parts: list[str] = []
            variables: dict[str, Any] = {}
            for i, (key, value) in enumerate(data.items()):
                validate_field_name(key, "relation data field")
                var_name = f"_rd{i}"
                set_parts.append(f"{key} = ${var_name}")
                variables[var_name] = value
            set_clause = " SET " + ", ".join(set_parts)
            query = f"RELATE {source}->{edge}->{target_thing}{set_clause};"
        else:
            variables = {}
            query = f"RELATE {source}->{edge}->{target_thing};"

        client = await SurrealDBConnectionManager.get_client()
        result = await client.query(remove_quotes_for_variables(query), variables)
        return result if isinstance(result, list) else []

    async def remove_relation(
        self,
        edge: str,
        target: "BaseSurrealModel | str",
    ) -> None:
        """
        Remove a specific relation between this record and the target.

        Args:
            edge: The relation/edge table name.
            target: The target model instance or ``"table:id"`` string.

        Example::

            await user.remove_relation("follows", other_user)
            await user.remove_relation("follows", "User:bob")
        """
        validate_edge_name(edge)
        source = self._get_thing()
        target_thing = self._resolve_target_thing(target)

        query = f"DELETE {edge} WHERE in = {source} AND out = {target_thing};"
        client = await SurrealDBConnectionManager.get_client()
        with contextlib.suppress(NotFoundError):
            await client.query(query, {})

    async def remove_all_relations(
        self,
        edge: str,
        *,
        direction: str = "out",
    ) -> None:
        """
        Remove all relations of a given type from or to this record.

        Args:
            edge: The relation/edge table name.
            direction: ``"out"`` removes outgoing relations (default),
                       ``"in"`` removes incoming relations,
                       ``"both"`` removes all relations involving this record.

        Example::

            await user.remove_all_relations("follows", direction="out")
            await user.remove_all_relations("follows", direction="both")
        """
        validate_edge_name(edge)
        thing = self._get_thing()

        if direction == "out":
            query = f"DELETE {edge} WHERE in = {thing};"
        elif direction == "in":
            query = f"DELETE {edge} WHERE out = {thing};"
        elif direction == "both":
            query = f"DELETE {edge} WHERE in = {thing} OR out = {thing};"
        else:
            raise ValueError(f"direction must be 'out', 'in', or 'both', got '{direction}'")

        client = await SurrealDBConnectionManager.get_client()
        with contextlib.suppress(NotFoundError):
            await client.query(query, {})

    async def get_related(
        self,
        edge: str,
        *,
        direction: str = "out",
        model_class: type["BaseSurrealModel"] | None = None,
    ) -> list[Any]:
        """
        Get related records through a relation edge.

        Args:
            edge: The relation/edge table name.
            direction: ``"out"`` for outgoing relations (default),
                       ``"in"`` for incoming relations.
            model_class: Optional model class to deserialize results into.

        Returns:
            A list of related records (model instances if model_class is provided,
            otherwise raw dicts/values).

        Example::

            following = await user.get_related("follows", direction="out", model_class=User)
            followers = await user.get_related("follows", direction="in", model_class=User)
        """
        validate_edge_name(edge)
        thing = self._get_thing()

        if direction == "out":
            query = f"SELECT VALUE ->{edge}->? FROM ONLY {thing};"
        elif direction == "in":
            query = f"SELECT VALUE <-{edge}<-? FROM ONLY {thing};"
        else:
            raise ValueError(f"direction must be 'out' or 'in', got '{direction}'")

        client = await SurrealDBConnectionManager.get_client()
        results = await client.query(query, {})

        if not isinstance(results, list) or len(results) == 0:
            return []

        # Results from SELECT VALUE ->edge->? are a flat list of record IDs/objects
        records = results

        if model_class is not None:
            # If results are RecordIDs, we need to SELECT them
            if records and isinstance(records[0], RecordID):
                # RecordIDs come from the database (typed/trusted) — bind them as a
                # query variable instead of string-interpolating, which both avoids
                # injection risk and the SurrealQL-escaped string form (e.g. "Table:⟨1⟩")
                # that validate_thing would reject.
                fetch_query = "SELECT * FROM $ids;"
                records = await client.query(fetch_query, {"ids": list(records)})
                if not isinstance(records, list):
                    return []

            try:
                return model_class.from_db(records)  # type: ignore
            except (ValueError, TypeError):
                return records

        return records

    async def traverse(self, path: str) -> list[Any]:
        """
        Execute a graph traversal from this record.

        Args:
            path: A SurrealQL graph traversal path starting with ``->`` or ``<-``.

        Returns:
            A list of records found by the traversal.

        Example::

            # Friends of friends
            fof = await user.traverse("->follows->User->follows->User")
        """
        validate_graph_path(path)
        thing = self._get_thing()

        query = f"SELECT VALUE {path} FROM ONLY {thing};"
        client = await SurrealDBConnectionManager.get_client()
        results = await client.query(query, {})

        if isinstance(results, list):
            return results
        return []

    @classmethod
    def objects(cls, tx: Transaction | None = None) -> Any:
        """
        Return a QuerySet for the model class.

        When ``tx`` is provided, the QuerySet's reads and bulk operations participate in
        that transaction (reads require an interactive transaction — WS + SurrealDB 3.x).
        """
        from .query_set import QuerySet

        qs = QuerySet(cls)
        if tx is not None:
            qs._tx = tx
        return qs

    @classmethod
    async def raw_query(
        cls,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> list[Self] | list[dict[str, Any]]:
        """
        Execute a raw SurrealQL query and return the results.

        This method allows executing arbitrary SurrealQL queries directly against
        the database. It's useful for complex queries that can't be expressed
        using the QuerySet API.

        Args:
            query: The SurrealQL query string to execute.
            variables: Optional dictionary of variables to substitute into the query.
                      Use $variable_name syntax in the query string.

        Returns:
            list[Self] | list[dict]: A list of model instances if the results match
            the model schema, otherwise a list of dictionaries.

        Example:
            ```python
            # Simple query
            users = await User.raw_query("SELECT * FROM User WHERE age > 21")

            # With variables (safe from injection)
            users = await User.raw_query(
                "SELECT * FROM User WHERE status = $status AND age > $min_age",
                variables={"status": "active", "min_age": 18}
            )

            # Complex graph query
            results = await User.raw_query('''
                SELECT *, ->purchased->Product AS products
                FROM User
                WHERE id = $user_id
            ''', variables={"user_id": "user:123"})
            ```
        """
        from .utils import remove_quotes_for_variables

        client = await SurrealDBConnectionManager.get_client()
        results = await client.query(
            remove_quotes_for_variables(query),
            variables or {},
        )

        # SDK 2.0 returns the rows list directly from query()
        if isinstance(results, list):
            try:
                return cls.from_db(results)  # type: ignore
            except (ValueError, TypeError, ValidationError):
                # If validation fails, return raw dicts
                return results

        return []
