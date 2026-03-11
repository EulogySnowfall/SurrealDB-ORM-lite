import logging
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_core import ValidationError
from surrealdb import RecordID

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
        Get the ID of the model instance.
        """
        if hasattr(self, "id"):
            id_value = self.id  # type: ignore[attr-defined]
            return str(id_value) if id_value is not None else None

        if hasattr(self, "model_config"):
            primary_key = self.model_config.get("primary_key", None)
            if isinstance(primary_key, str) and hasattr(self, primary_key):
                primary_key_value = getattr(self, primary_key)
                return str(primary_key_value) if primary_key_value is not None else None

        return None  # pragma: no cover

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
        Extracts the ID from RecordID if present.
        """
        if isinstance(data, dict) and "id" in data and isinstance(data["id"], RecordID):
            data["id"] = str(data["id"]).split(":")[1]
        return data

    async def refresh(self) -> None:
        """
        Refresh the model instance from the database.
        """
        if not self.get_id():
            raise SurrealDbError("Can't refresh data, not recorded yet.")  # pragma: no cover

        client = await SurrealDBConnectionManager.get_client()
        record = await client.select(f"{self.get_table_name()}:{self.get_id()}")

        if record is None:
            raise SurrealDbError("Can't refresh data, no record found.")  # pragma: no cover

        # SDK 1.0.8 returns a list even for single record select
        if isinstance(record, list):
            if len(record) == 0:
                raise SurrealDbError("Can't refresh data, no record found.")  # pragma: no cover
            record = record[0]

        # Update current instance with refreshed data
        if isinstance(record, dict):
            for key, value in record.items():
                if key == "id" and isinstance(value, RecordID):
                    value = str(value).split(":")[1]
                if hasattr(self, key):
                    object.__setattr__(self, key, value)

    async def _do_save(self) -> tuple[Self, bool]:
        """
        Internal save logic. Returns (self, created) where created indicates
        whether a new record was created (always True for save).
        """
        client = await SurrealDBConnectionManager.get_client()
        data = self.model_dump(exclude={"id"})
        id = self.get_id()
        table = self.get_table_name()

        if id is not None:
            # Escape special characters in ID
            escaped_id = f"`{id}`" if any(c in str(id) for c in "@#$%^&*()-+=/\\! ") else id
            thing = f"{table}:{escaped_id}"
            result = await client.create(thing, data)
            # SDK 1.0.8 returns error message as string instead of raising exception
            if isinstance(result, str) and "already exists" in result:
                raise SurrealDbError(f"There was a problem with the database: {result}")
            return self, True

        # Auto-generate the ID
        record = await client.create(table, data)  # pragma: no cover

        # SDK 1.0.8 returns error message as string
        if isinstance(record, str):
            raise SurrealDbError(f"Can't save data: {record}")  # pragma: no cover

        if isinstance(record, list):
            raise SurrealDbError("Can't save data, multiple records returned.")  # pragma: no cover

        if record is None:
            raise SurrealDbError("Can't save data, no record returned.")  # pragma: no cover

        # Update current instance with the auto-generated ID
        if isinstance(record, dict):
            for key, value in record.items():
                if key == "id" and isinstance(value, RecordID):
                    value = str(value).split(":")[1]
                if hasattr(self, key):
                    object.__setattr__(self, key, value)
            return self, True

        raise SurrealDbError("Can't save data, no record returned.")  # pragma: no cover

    async def save(self) -> Self:
        """
        Save the model instance to the database.

        Emits pre_save, post_save, and around_save signals.
        """
        sender = self.__class__
        has_signals = pre_save.has_handlers(sender) or post_save.has_handlers(sender) or around_save.has_handlers(sender)

        if not has_signals:
            result, created = await self._do_save()
            return result

        await pre_save.send(sender, instance=self)

        async with around_save.wrap(sender, instance=self):
            result, created = await self._do_save()

        await post_save.send(sender, instance=self, created=created)

        return result

    async def update(self) -> Any:
        """
        Update the model instance to the database.

        Emits pre_update, post_update, and around_update signals.
        """
        client = await SurrealDBConnectionManager.get_client()
        sender = self.__class__

        data = self.model_dump(exclude={"id"})
        id = self.get_id()
        if id is not None:
            thing = f"{self.__class__.__name__}:{id}"
            update_fields = list(data.keys())
            has_signals = (
                pre_update.has_handlers(sender) or post_update.has_handlers(sender) or around_update.has_handlers(sender)
            )

            if not has_signals:
                return await client.update(thing, data)

            await pre_update.send(sender, instance=self, update_fields=update_fields)

            async with around_update.wrap(sender, instance=self, update_fields=update_fields):
                result = await client.update(thing, data)

            await post_update.send(sender, instance=self, update_fields=update_fields)

            return result
        raise SurrealDbError("Can't update data, no id found.")

    async def merge(self, **data: Any) -> Any:
        """
        Partial update of the model instance in the database.

        Emits pre_update, post_update, and around_update signals.
        """

        client = await SurrealDBConnectionManager.get_client()
        sender = self.__class__
        data_set = dict(data.items())

        id = self.get_id()
        if id is not None:
            thing = f"{self.get_table_name()}:{id}"
            update_fields = list(data_set.keys())
            has_signals = (
                pre_update.has_handlers(sender) or post_update.has_handlers(sender) or around_update.has_handlers(sender)
            )

            if not has_signals:
                await client.merge(thing, data_set)
                await self.refresh()
                return

            await pre_update.send(sender, instance=self, update_fields=update_fields)

            async with around_update.wrap(sender, instance=self, update_fields=update_fields):
                await client.merge(thing, data_set)
                await self.refresh()

            await post_update.send(sender, instance=self, update_fields=update_fields)

            return

        raise SurrealDbError(f"No Id for the data to merge: {data}")

    async def delete(self) -> None:
        """
        Delete the model instance from the database.

        Emits pre_delete, post_delete, and around_delete signals.
        """

        client = await SurrealDBConnectionManager.get_client()
        sender = self.__class__

        id = self.get_id()
        if id is None:
            raise SurrealDbError("Can't delete data, no id found.")

        thing = f"{self.get_table_name()}:{id}"
        has_signals = pre_delete.has_handlers(sender) or post_delete.has_handlers(sender) or around_delete.has_handlers(sender)

        if not has_signals:
            deleted = await client.delete(thing)
            if not deleted:
                raise SurrealDbError(f"Can't delete Record id -> '{id}' not found!")
            logger.info(f"Record deleted -> {deleted}.")
            return

        await pre_delete.send(sender, instance=self)

        async with around_delete.wrap(sender, instance=self):
            deleted = await client.delete(thing)

            if not deleted:
                raise SurrealDbError(f"Can't delete Record id -> '{id}' not found!")

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
                # RecordIDs come from the database, validate each one before interpolation
                ids = []
                for r in records:
                    rid = str(r)
                    validate_thing(rid)
                    ids.append(rid)
                placeholders = ", ".join(ids)
                fetch_query = f"SELECT * FROM {placeholders};"
                records = await client.query(fetch_query, {})
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

        # SDK 1.0.8 returns list directly from query()
        if isinstance(results, list):
            try:
                return cls.from_db(results)  # type: ignore
            except (ValueError, TypeError, ValidationError):
                # If validation fails, return raw dicts
                return results

        return []
