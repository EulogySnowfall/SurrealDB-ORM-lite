import logging
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, model_validator
from surrealdb import RecordID

from .connection_manager import SurrealDBConnectionManager
from .exceptions import SurrealDbError

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
            id_value = self.id
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

    async def save(self) -> Self:
        """
        Save the model instance to the database.
        """
        client = await SurrealDBConnectionManager.get_client()
        data = self.model_dump(exclude={"id"})
        id = self.get_id()
        table = self.get_table_name()

        if id is not None:
            # Escape special characters in ID
            escaped_id = f"`{id}`" if any(c in str(id) for c in "@#$%^&*()") else id
            thing = f"{table}:{escaped_id}"
            result = await client.create(thing, data)
            # SDK 1.0.8 returns error message as string instead of raising exception
            if isinstance(result, str) and "already exists" in result:
                raise SurrealDbError(f"There was a problem with the database: {result}")
            return self

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
            return self

        raise SurrealDbError("Can't save data, no record returned.")  # pragma: no cover

    async def update(self) -> Any:
        """
        Update the model instance to the database.
        """
        client = await SurrealDBConnectionManager.get_client()

        data = self.model_dump(exclude={"id"})
        id = self.get_id()
        if id is not None:
            thing = f"{self.__class__.__name__}:{id}"
            result = await client.update(thing, data)
            return result
        raise SurrealDbError("Can't update data, no id found.")

    async def merge(self, **data: Any) -> Any:
        """
        Update the model instance to the database.
        """

        client = await SurrealDBConnectionManager.get_client()
        data_set = dict(data.items())

        id = self.get_id()
        if id:
            thing = f"{self.get_table_name()}:{id}"

            await client.merge(thing, data_set)
            await self.refresh()
            return

        raise SurrealDbError(f"No Id for the data to merge: {data}")

    async def delete(self) -> None:
        """
        Delete the model instance from the database.
        """

        client = await SurrealDBConnectionManager.get_client()

        id = self.get_id()

        thing = f"{self.get_table_name()}:{id}"

        deleted = await client.delete(thing)

        if not deleted:
            raise SurrealDbError(f"Can't delete Record id -> '{id}' not found!")

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

    @classmethod
    def objects(cls) -> Any:
        """
        Return a QuerySet for the model class.
        """
        from .query_set import QuerySet

        return QuerySet(cls)
