import contextlib
import logging
import typing
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_core import ValidationError

from ._sdk import NotFoundError, RecordID, ServerError
from .connection_manager import SurrealDBConnectionManager
from .exceptions import SurrealDbError
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
from .utils import remove_quotes_for_variables, validate_edge_name, validate_field_name, validate_graph_path, validate_thing

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

        Reads inside a transaction are not supported in v0.8.0 (planned for v0.9.0).
        """
        if tx is not None:
            raise SurrealDbError("refresh() is a read and is not supported inside a transaction (planned for v0.9.0).")
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
            if record_id is None:
                raise SurrealDbError("save(tx=...) requires an explicit id (auto-id is not supported inside a transaction).")
            tx.add(f"CREATE {record_id} CONTENT $data;", {"data": data})
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

    async def save(self, tx: Transaction | None = None) -> Self:
        """
        Save the model instance to the database.

        When ``tx`` is provided, the CREATE statement is buffered onto the
        transaction instead of being executed immediately.

        Emits pre_save and post_save signals. ``around_save`` wraps the actual write,
        so in a transaction (``tx`` provided) it is NOT emitted — the write happens at
        commit, not here — consistent with update/merge/delete in tx mode.
        """
        sender = self.__class__
        has_signals = pre_save.has_handlers(sender) or post_save.has_handlers(sender) or around_save.has_handlers(sender)

        if not has_signals:
            result, created = await self._do_save(tx=tx)
            return result

        await pre_save.send(sender, instance=self)

        if tx is not None:
            # Buffered op: there is no actual write to wrap here, so around_save is
            # skipped (the write happens at commit). pre_save/post_save still fire.
            result, created = await self._do_save(tx=tx)
        else:
            async with around_save.wrap(sender, instance=self):
                result, created = await self._do_save(tx=tx)

        await post_save.send(sender, instance=self, created=created)

        return result

    async def update(self, tx: Transaction | None = None) -> Any:
        """
        Update the model instance to the database.

        When ``tx`` is provided, the UPDATE statement is buffered onto the
        transaction instead of being executed immediately.

        Emits pre_update, post_update, and around_update signals.
        """
        sender = self.__class__
        data = self.model_dump(exclude={"id"})
        record_id = self._record_id()
        if record_id is None:
            raise SurrealDbError("Can't update data, no id found.")

        if tx is not None:
            update_fields = list(data.keys())
            await pre_update.send(sender, instance=self, update_fields=update_fields)
            tx.add(f"UPDATE {record_id} CONTENT $data;", {"data": data})
            await post_update.send(sender, instance=self, update_fields=update_fields)
            return None

        client = await SurrealDBConnectionManager.get_client()
        update_fields = list(data.keys())
        has_signals = pre_update.has_handlers(sender) or post_update.has_handlers(sender) or around_update.has_handlers(sender)

        if not has_signals:
            return await client.update(record_id, data)

        await pre_update.send(sender, instance=self, update_fields=update_fields)

        async with around_update.wrap(sender, instance=self, update_fields=update_fields):
            result = await client.update(record_id, data)

        await post_update.send(sender, instance=self, update_fields=update_fields)

        return result

    async def merge(self, tx: Transaction | None = None, **data: Any) -> Any:
        """
        Partial update of the model instance in the database.

        When ``tx`` is provided, the UPDATE…MERGE statement is buffered onto the
        transaction instead of being executed immediately.

        Note: ``tx`` is a reserved keyword argument for this method. A model field
        literally named ``tx`` cannot be merged by keyword; use a dict-unpacking
        workaround if needed (no realistic SurrealDB column is named ``tx``).

        Emits pre_update, post_update, and around_update signals.
        """
        sender = self.__class__
        data_set = dict(data.items())
        record_id = self._record_id()

        if record_id is None:
            raise SurrealDbError(f"No Id for the data to merge: {data}")

        if tx is not None:
            update_fields = list(data_set.keys())
            await pre_update.send(sender, instance=self, update_fields=update_fields)
            tx.add(f"UPDATE {record_id} MERGE $data;", {"data": data_set})
            await post_update.send(sender, instance=self, update_fields=update_fields)
            return None

        client = await SurrealDBConnectionManager.get_client()
        update_fields = list(data_set.keys())
        has_signals = pre_update.has_handlers(sender) or post_update.has_handlers(sender) or around_update.has_handlers(sender)

        if not has_signals:
            await client.merge(record_id, data_set)
            await self.refresh()
            return

        await pre_update.send(sender, instance=self, update_fields=update_fields)

        async with around_update.wrap(sender, instance=self, update_fields=update_fields):
            await client.merge(record_id, data_set)
            await self.refresh()

        await post_update.send(sender, instance=self, update_fields=update_fields)

    async def delete(self, tx: Transaction | None = None) -> None:
        """
        Delete the model instance from the database.

        When ``tx`` is provided, the DELETE statement is buffered onto the
        transaction instead of being executed immediately.

        Emits pre_delete, post_delete, and around_delete signals.
        """
        sender = self.__class__
        record_id = self._record_id()
        if record_id is None:
            raise SurrealDbError("Can't delete data, no id found.")

        if tx is not None:
            await pre_delete.send(sender, instance=self)
            tx.add(f"DELETE {record_id};", None)
            await post_delete.send(sender, instance=self)
            return None

        client = await SurrealDBConnectionManager.get_client()
        has_signals = pre_delete.has_handlers(sender) or post_delete.has_handlers(sender) or around_delete.has_handlers(sender)

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
    def objects(cls) -> Any:
        """
        Return a QuerySet for the model class.
        """
        from .query_set import QuerySet

        return QuerySet(cls)

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
