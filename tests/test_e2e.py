import os

import pytest
from pydantic import Field
from surrealdb import RecordID

from src import surreal_orm_lite
from src.surreal_orm_lite.exceptions import SurrealDbError, SurrealDbNotFoundError

SURREALDB_HOST = os.environ.get("SURREALDB_HOST", "localhost")
SURREALDB_PORT = os.environ.get("SURREALDB_PORT", "8000")
SURREALDB_URL = f"http://{SURREALDB_HOST}:{SURREALDB_PORT}"
SURREALDB_USER = "root"
SURREALDB_PASS = "root"
SURREALDB_NAMESPACE = "ns"
SURREALDB_DATABASE = "db"


class ModelTest(surreal_orm_lite.BaseSurrealModel):
    id: str | RecordID | None = None
    name: str = Field(..., max_length=100)
    age: int = Field(..., ge=0, le=125)


class ModelTestEmpty(surreal_orm_lite.BaseSurrealModel):
    id: str | RecordID | None = Field(default=None)
    name: str = Field(..., max_length=100)
    age: int = Field(..., ge=0, le=125)


class ModelNeverCreated(surreal_orm_lite.BaseSurrealModel):
    """Used only for read queries so its table is never created in the DB.

    SurrealDB 3.x raises NotFoundError ("table does not exist") for reads on a
    table that was never written to, whereas the ORM contract treats a missing
    table as empty. This model guards that contract.
    """

    id: str | RecordID | None = Field(default=None)
    name: str = Field(..., max_length=100)
    age: int = Field(..., ge=0, le=125)


@pytest.fixture(scope="module", autouse=True)
def setup_surrealdb() -> None:
    # Initialiser SurrealDB
    surreal_orm_lite.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )


async def test_save_model() -> None:
    model = ModelTest(id="1", name="Test Man", age=42)
    await model.save()

    # Vérification de l'insertion
    client = await surreal_orm_lite.SurrealDBConnectionManager.get_client()
    result = await client.select("ModelTest")
    test_id = RecordID(table_name="ModelTest", identifier="1")
    assert len(result) == 1

    assert result[0] == {"id": test_id, "name": "Test Man", "age": 42}


async def test_merge_model() -> None:
    item = await ModelTest.objects().get("1")
    assert item.name == "Test Man"
    assert item.age == 42
    await item.merge(age=32)  # Also test whole refresh() method
    item.age = 32
    item.name = "Test Man"
    item.id = "1"

    item2 = await ModelTest.objects().filter(name="Test Man").get()
    assert item2.age == 32
    assert item2.name == "Test Man"
    assert item2.get_raw_id() == "1"


async def test_update_model() -> None:
    item = await ModelTest.objects().get("1")
    assert item.name == "Test Man"
    assert item.age == 32
    item.age = 25
    await item.update()
    item2 = await ModelTest.objects().get("1")
    assert item2.age == 25

    item2 = await ModelTest.objects().filter(name="Test Man").get()
    assert item2.age == 25
    assert item2.name == "Test Man"
    assert item2.get_raw_id() == "1"

    item3 = ModelTest(name="TestNone", age=17)

    with pytest.raises(SurrealDbError) as exc1:
        await item3.update()

    assert str(exc1.value) == "Can't update data, no id found."

    with pytest.raises(SurrealDbError) as exc2:
        await item3.refresh()

    assert str(exc2.value) == "Can't refresh data, not recorded yet."  # test Error in refresh()

    with pytest.raises(SurrealDbError) as exc2:
        await item3.merge(age=19)

    assert str(exc2.value) == "No Id for the data to merge: {'age': 19}"


async def test_first_model() -> None:
    model = await ModelTest.objects().filter(name="Test Man").first()
    assert isinstance(model, ModelTest), "Expected ModelTest instance"
    assert model.name == "Test Man"
    assert model.age == 25
    assert model.get_raw_id() == "1"

    with pytest.raises(SurrealDbNotFoundError) as exc1:
        await ModelTest.objects().filter(name="NotExist").first()

    assert str(exc1.value) == "No result found."


async def test_filter_model() -> None:
    item3 = ModelTest(name="Test2", age=17)
    await item3.save()

    models = await ModelTest.objects().filter(age__lt=30).exec()  # Test from_db isinstance(record["id"], RecordID)
    assert len(models) == 2
    for model in models:
        assert model.age < 30


async def test_save_model_already_exist() -> None:
    model = ModelTest(id="1", name="Test2", age=34)
    with pytest.raises(SurrealDbError) as exc:
        await model.save()

    assert "already exists" in str(exc.value)


async def test_delete_model() -> None:
    model = ModelTest(id="4", name="Test2", age=34)
    await model.save()
    client = await surreal_orm_lite.SurrealDBConnectionManager.get_client()
    result = await client.select("ModelTest")
    assert len(result) == 3

    await model.delete()
    result = await client.select("ModelTest")
    assert len(result) == 2

    model2 = ModelTest(id="345", name="Test2", age=34)

    with pytest.raises(SurrealDbError) as exc1:
        await model2.delete()  # Test delete() without saved()

    assert "not found" in str(exc1.value)


async def test_query_model() -> None:
    # Utiliser test_model pour exécuter la requête
    results = await ModelTest.objects().filter(name="Test Man").exec()
    assert len(results) == 1
    assert results[0].name == "Test Man"


async def test_multi_select() -> None:
    await ModelTest(name="Ian", age=23).save()
    await ModelTest(name="Yan", age=32).save()
    await ModelTest(name="Isa", age=32).save()

    result = await ModelTest.objects().all()

    assert len(result) == 5

    result1 = await ModelTest.objects().filter(name__in=["Ian", "Yan"]).exec()

    assert len(result1) == 2
    for item in result1:
        assert item.name in ["Yan", "Ian"]

    # Test order_by
    result2 = await ModelTest.objects().order_by("name").exec()
    assert len(result2) == 5
    assert result2[0].name == "Ian"

    # Test order_by DESC
    result3 = await ModelTest.objects().order_by("name", surreal_orm_lite.OrderBy.DESC).exec()
    assert len(result3) == 5
    assert result3[0].name == "Yan"

    # Test offset and limit
    result4 = await ModelTest.objects().offset(2).exec()
    assert len(result4) == 3

    result5 = await ModelTest.objects().limit(2).exec()
    assert len(result5) == 2

    # Select only age
    result6 = await ModelTest.objects().select("age").exec()
    assert len(result6) == 5
    assert isinstance(result6[0], dict)

    result7 = await ModelTest.objects().filter(age__lte="$max_age").variables(max_age=25).exec()
    assert len(result7) == 3
    for res in result7:
        assert res.age <= 25

    result8 = await ModelTest.objects().query("SELECT * FROM ModelTest WHERE age > 25")
    assert len(result8) == 2
    for res in result8:
        assert res.age > 25

    result9 = await ModelTest.objects().query("SELECT * FROM ModelTest WHERE age > $age", {"age": 19})
    assert len(result9) == 4

    with pytest.raises(SurrealDbError) as exc:
        await ModelTest.objects().query("SELECT * FROM NoTable WHERE age > 34")

    assert str(exc.value) == "The query must include 'FROM ModelTest' to reference the correct table."


async def test_error_on_get_multi() -> None:
    with pytest.raises(SurrealDbError) as exc1:
        await ModelTest.objects().get()

    assert str(exc1.value) == "More than one result found."

    with pytest.raises(SurrealDbNotFoundError) as exc2:
        await ModelTestEmpty.objects().get()

    assert str(exc2.value) == "No result found."


async def test_read_on_never_created_table_is_empty() -> None:
    """Reads on a table that was never created honor the empty-result contract.

    Regression for SurrealDB 3.x raising NotFoundError instead of returning an
    empty result set (the table is auto-created on first write only).
    """
    qs = ModelNeverCreated.objects

    with pytest.raises(SurrealDbNotFoundError):
        await qs().get()

    with pytest.raises(SurrealDbNotFoundError):
        await qs().first()

    with pytest.raises(SurrealDbNotFoundError):
        await qs().get("does-not-exist")

    assert await qs().count() == 0
    assert await qs().exists() is False
    assert await qs().all() == []
    assert await qs().filter(age__gt=1).exec() == []
    assert await qs().sum("age") == 0
    assert await qs().avg("age") == 0.0
    assert await qs().min("age") is None
    assert await qs().max("age") is None
    assert await qs().query("SELECT * FROM ModelNeverCreated") == []


async def test_with_primary_key() -> None:
    class ModelTest2(surreal_orm_lite.BaseSurrealModel):
        model_config = surreal_orm_lite.SurrealConfigDict(primary_key="email")
        name: str = Field(..., max_length=100)
        age: int = Field(..., ge=0, le=125)
        email: str = Field(..., max_length=100)

    # Clean up from previous test runs
    await ModelTest2.objects().delete_table()

    model = ModelTest2(name="Test", age=32, email="test@test.com")
    await model.save()

    # Error on duplicate primary key
    with pytest.raises(SurrealDbError) as exc:
        await ModelTest2(name="Test3", age=35, email="test@test.com").save()

    assert "already exists" in str(exc.value)

    # Use backticks to escape special characters in ID for get()
    fletch = await ModelTest2.objects().get("`test@test.com`")
    assert isinstance(fletch, ModelTest2), "Expected ModelTest2 instance"
    assert fletch.name == "Test"
    assert fletch.age == 32
    assert fletch.email == "test@test.com"

    deleted = await ModelTest2.objects().delete_table()
    assert deleted is True


async def test_save_with_auto_generated_id() -> None:
    """Test save() when id is None (auto-generated by SurrealDB)."""
    model = ModelTestEmpty(name="AutoGenerated", age=99)
    assert model.id is None

    await model.save()

    # After save, the model should have an auto-generated ID
    assert model.id is not None
    assert model.name == "AutoGenerated"
    assert model.age == 99

    # Verify it was saved in the database
    fetched = await ModelTestEmpty.objects().filter(name="AutoGenerated").first()
    assert fetched.name == "AutoGenerated"
    assert fetched.age == 99

    # Clean up
    await ModelTestEmpty.objects().delete_table()


async def test_filter_contains() -> None:
    """Test the contains lookup operator."""
    # Create test data
    await ModelTest(id="c1", name="John Smith", age=30).save()
    await ModelTest(id="c2", name="Jane Doe", age=25).save()
    await ModelTest(id="c3", name="Johnny Walker", age=40).save()

    # Test contains lookup
    results = await ModelTest.objects().filter(name__contains="John").exec()
    assert len(results) == 2
    for r in results:
        assert "John" in r.name

    # Clean up
    await ModelTest.objects().delete_table()


async def test_duplicate_save_raises_surreal_error() -> None:
    import contextlib

    from src.surreal_orm_lite.exceptions import SurrealDbError

    class DupModel(surreal_orm_lite.BaseSurrealModel):
        id: str | RecordID | None = None
        name: str

    # Ensure a clean slate so the test is idempotent across reruns/servers.
    await DupModel.objects().delete_table()

    await DupModel(id="dup_x", name="first").save()
    with pytest.raises(SurrealDbError):
        await DupModel(id="dup_x", name="second").save()
    with contextlib.suppress(SurrealDbError):
        await DupModel(id="dup_x", name="first").delete()


async def test_delete_table() -> None:
    # Suppression de la table via test_model
    result = await ModelTest.objects().delete_table()
    assert result is True


async def test_loaded_record_keeps_native_recordid() -> None:
    class RidUser(surreal_orm_lite.BaseSurrealModel):
        id: str | RecordID | None = None
        name: str

    await RidUser(id="rid_keep", name="Keep").save()
    loaded = await RidUser.objects().get("rid_keep")
    assert isinstance(loaded.id, RecordID)
    assert loaded.id.table_name == "RidUser"
    assert str(loaded.id.id) == "rid_keep"
    await loaded.delete()


async def test_delete_table_missing_is_noop() -> None:
    class NeverCreated(surreal_orm_lite.BaseSurrealModel):
        id: str | RecordID | None = None
        name: str

    # Table was never created; delete_table must not raise on 3.x.
    assert await NeverCreated.objects().delete_table() is True


async def test_queryset_get_by_id_returns_native_recordid() -> None:
    class GetRid(surreal_orm_lite.BaseSurrealModel):
        id: str | RecordID | None = None
        name: str

    await GetRid(id="g1", name="G").save()
    obj = await GetRid.objects().get("g1")
    assert isinstance(obj.id, RecordID)
    await obj.delete()


async def test_update_merge_delete_with_recordid_roundtrip() -> None:
    class RidCrud(surreal_orm_lite.BaseSurrealModel):
        id: str | RecordID | None = None
        name: str
        age: int = 0

    obj = RidCrud(id="rid_crud", name="A", age=1)
    await obj.save()

    loaded = await RidCrud.objects().get("rid_crud")  # id is a RecordID here
    loaded.age = 2
    await loaded.update()
    await loaded.merge(name="B")

    again = await RidCrud.objects().get("rid_crud")
    assert again.age == 2
    assert again.name == "B"

    await again.delete()
    with pytest.raises(SurrealDbNotFoundError):
        await RidCrud.objects().get("rid_crud")
