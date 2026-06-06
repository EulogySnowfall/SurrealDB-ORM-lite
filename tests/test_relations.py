"""
Tests for v0.6.0 features: Relations & Graph (relate, remove_relation,
get_related, remove_all_relations, traverse, FETCH clause).
"""

import contextlib
import os

import pytest
from pydantic import Field
from surrealdb import NotFoundError, RecordID

from src import surreal_orm_lite
from src.surreal_orm_lite.exceptions import SurrealDbError
from src.surreal_orm_lite.utils import validate_edge_name, validate_graph_path

# =============================================================================
# Test Models
# =============================================================================


class Person(surreal_orm_lite.BaseSurrealModel):
    id: str | RecordID | None = None
    name: str = Field(..., max_length=100)
    age: int = Field(default=0, ge=0)


class Product(surreal_orm_lite.BaseSurrealModel):
    id: str | RecordID | None = None
    name: str = Field(..., max_length=100)
    price: float = Field(default=0.0, ge=0)


# =============================================================================
# Unit Tests - Validation
# =============================================================================


class TestEdgeValidation:
    """Unit tests for edge name validation."""

    def test_valid_edge_name(self) -> None:
        validate_edge_name("follows")
        validate_edge_name("purchased")
        validate_edge_name("has_tag")
        validate_edge_name("_internal")

    def test_empty_edge_name(self) -> None:
        with pytest.raises(ValueError, match="edge name cannot be empty"):
            validate_edge_name("")

    def test_whitespace_edge_name(self) -> None:
        with pytest.raises(ValueError, match="edge name cannot be empty"):
            validate_edge_name("   ")

    def test_invalid_edge_name_special_chars(self) -> None:
        with pytest.raises(ValueError, match="Invalid edge name"):
            validate_edge_name("follows;DROP")

    def test_invalid_edge_name_spaces(self) -> None:
        with pytest.raises(ValueError, match="Invalid edge name"):
            validate_edge_name("my edge")

    def test_invalid_edge_name_dots(self) -> None:
        with pytest.raises(ValueError, match="Invalid edge name"):
            validate_edge_name("edge.nested")


class TestGraphPathValidation:
    """Unit tests for graph path validation."""

    def test_valid_path(self) -> None:
        validate_graph_path("->follows->Person")
        validate_graph_path("<-follows<-Person")
        validate_graph_path("->follows->Person->likes->Product")

    def test_empty_path(self) -> None:
        with pytest.raises(ValueError, match="graph path cannot be empty"):
            validate_graph_path("")

    def test_invalid_path_injection(self) -> None:
        with pytest.raises(ValueError, match="Invalid graph path"):
            validate_graph_path("->follows; DROP TABLE users--")

    def test_invalid_path_no_arrow_prefix(self) -> None:
        with pytest.raises(ValueError, match="Invalid graph path"):
            validate_graph_path("follows->User")

    def test_invalid_path_arbitrary_chars(self) -> None:
        with pytest.raises(ValueError, match="Invalid graph path"):
            validate_graph_path(">>>>>")

    def test_invalid_path_double_dash(self) -> None:
        with pytest.raises(ValueError, match="Invalid graph path"):
            validate_graph_path("---<><>")


# =============================================================================
# Unit Tests - Model Method Validation
# =============================================================================


class TestRelateValidation:
    """Unit tests for relate() input validation (no DB needed)."""

    def test_get_thing_no_id(self) -> None:
        person = Person(name="Test")
        with pytest.raises(SurrealDbError, match="unsaved model"):
            person._get_thing()

    def test_get_thing_with_id(self) -> None:
        person = Person(id="alice", name="Alice")
        assert person._get_thing() == "Person:alice"

    def test_resolve_target_thing_model(self) -> None:
        target = Person(id="bob", name="Bob")
        result = Person._resolve_target_thing(target)
        assert result == "Person:bob"

    def test_resolve_target_thing_string(self) -> None:
        result = Person._resolve_target_thing("Person:bob")
        assert result == "Person:bob"

    def test_resolve_target_thing_invalid_string(self) -> None:
        with pytest.raises(ValueError, match="record identifier"):
            Person._resolve_target_thing("just_an_id")

    def test_resolve_target_thing_injection(self) -> None:
        with pytest.raises(ValueError, match="record identifier"):
            Person._resolve_target_thing("User:alice; DELETE User WHERE true--")

    def test_resolve_target_thing_invalid_type(self) -> None:
        with pytest.raises(TypeError, match="BaseSurrealModel instance"):
            Person._resolve_target_thing(42)  # type: ignore


# =============================================================================
# Unit Tests - QuerySet.fetch()
# =============================================================================


class TestFetchClause:
    """Unit tests for QuerySet.fetch() clause building."""

    def test_fetch_adds_to_query(self) -> None:
        qs = Person.objects().fetch("author", "tags")
        query, _ = qs._compile_query()
        assert "FETCH author, tags" in query

    def test_fetch_with_filters(self) -> None:
        qs = Person.objects().filter(age__gte=18).fetch("friends")
        query, variables = qs._compile_query()
        assert "WHERE" in query
        assert "FETCH friends" in query
        assert "_f0" in variables

    def test_fetch_invalid_field(self) -> None:
        with pytest.raises(ValueError, match="Invalid"):
            Person.objects().fetch("bad;field")

    def test_fetch_position_in_query(self) -> None:
        """FETCH should appear after LIMIT/START."""
        qs = Person.objects().limit(10).offset(5).fetch("author")
        query, _ = qs._compile_query()
        limit_pos = query.index("LIMIT")
        start_pos = query.index("START")
        fetch_pos = query.index("FETCH")
        assert limit_pos < start_pos < fetch_pos


# =============================================================================
# E2E Tests - Relations (require SurrealDB)
# =============================================================================

SURREALDB_HOST = os.environ.get("SURREALDB_HOST", "localhost")
SURREALDB_PORT = os.environ.get("SURREALDB_PORT", "8000")
SURREALDB_URL = f"http://{SURREALDB_HOST}:{SURREALDB_PORT}"
SURREALDB_USER = "root"
SURREALDB_PASS = "root"
SURREALDB_NAMESPACE = "ns"
SURREALDB_DATABASE = "db"


@pytest.fixture(scope="module", autouse=True)
def setup_surrealdb() -> None:
    surreal_orm_lite.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )


class TestRelationsE2E:
    """E2E tests for relations (require running SurrealDB)."""

    async def test_relate_basic(self) -> None:
        """Create a basic relation between two records."""
        # Clean up
        await Person.objects().delete_table()
        client = await surreal_orm_lite.SurrealDBConnectionManager.get_client()
        with contextlib.suppress(NotFoundError):
            await client.query("DELETE follows;", {})

        alice = Person(id="alice", name="Alice", age=30)
        await alice.save()
        bob = Person(id="bob", name="Bob", age=25)
        await bob.save()

        result = await alice.relate("follows", bob)
        assert isinstance(result, list)
        assert len(result) > 0

    async def test_relate_with_data(self) -> None:
        """Create a relation with data on the edge."""
        client = await surreal_orm_lite.SurrealDBConnectionManager.get_client()
        with contextlib.suppress(NotFoundError):
            await client.query("DELETE purchased;", {})

        await Product.objects().delete_table()
        product = Product(id="widget", name="Widget", price=29.99)
        await product.save()

        alice = await Person.objects().get("alice")
        result = await alice.relate("purchased", product, data={"quantity": 2, "price": 29.99})
        assert isinstance(result, list)
        assert len(result) > 0

        # Verify data on the edge
        edges = await client.query("SELECT * FROM purchased;", {})
        assert len(edges) > 0
        edge = edges[0]
        assert edge["quantity"] == 2
        assert edge["price"] == 29.99

    async def test_relate_with_string_target(self) -> None:
        """Create a relation using a string target."""
        charlie = Person(id="charlie", name="Charlie", age=35)
        await charlie.save()

        alice = await Person.objects().get("alice")
        result = await alice.relate("follows", "Person:charlie")
        assert isinstance(result, list)
        assert len(result) > 0

    async def test_get_related_out(self) -> None:
        """Get outgoing related records."""
        alice = await Person.objects().get("alice")
        following = await alice.get_related("follows", direction="out", model_class=Person)
        assert isinstance(following, list)
        assert len(following) >= 1
        names = {p.name for p in following}
        assert "Bob" in names

    async def test_get_related_in(self) -> None:
        """Get incoming related records."""
        bob = await Person.objects().get("bob")
        followers = await bob.get_related("follows", direction="in", model_class=Person)
        assert isinstance(followers, list)
        assert len(followers) >= 1
        names = {p.name for p in followers}
        assert "Alice" in names

    async def test_get_related_no_model_class(self) -> None:
        """Get related without model_class returns raw data."""
        alice = await Person.objects().get("alice")
        following = await alice.get_related("follows", direction="out")
        assert isinstance(following, list)
        assert len(following) >= 1

    async def test_get_related_invalid_direction(self) -> None:
        """Invalid direction raises ValueError."""
        alice = await Person.objects().get("alice")
        with pytest.raises(ValueError, match="direction must be"):
            await alice.get_related("follows", direction="sideways")

    async def test_remove_relation(self) -> None:
        """Remove a specific relation."""
        alice = await Person.objects().get("alice")

        # Alice follows charlie - remove it
        await alice.remove_relation("follows", "Person:charlie")

        following = await alice.get_related("follows", direction="out", model_class=Person)
        names = {p.name for p in following}
        assert "Charlie" not in names

    async def test_remove_all_relations_out(self) -> None:
        """Remove all outgoing relations."""
        # First re-establish some relations
        alice = await Person.objects().get("alice")
        charlie = await Person.objects().get("charlie")
        await alice.relate("follows", charlie)

        # Now remove all outgoing follows
        await alice.remove_all_relations("follows", direction="out")

        following = await alice.get_related("follows", direction="out", model_class=Person)
        assert len(following) == 0

    async def test_remove_all_relations_invalid_direction(self) -> None:
        """Invalid direction raises ValueError."""
        alice = await Person.objects().get("alice")
        with pytest.raises(ValueError, match="direction must be"):
            await alice.remove_all_relations("follows", direction="sideways")

    async def test_traverse(self) -> None:
        """Basic graph traversal."""
        client = await surreal_orm_lite.SurrealDBConnectionManager.get_client()
        with contextlib.suppress(NotFoundError):
            await client.query("DELETE follows;", {})

        alice = await Person.objects().get("alice")
        bob = await Person.objects().get("bob")
        charlie = await Person.objects().get("charlie")

        await alice.relate("follows", bob)
        await bob.relate("follows", charlie)

        # Traverse: alice -> follows -> ? -> follows -> ?
        results = await alice.traverse("->follows->Person->follows->Person")
        assert isinstance(results, list)
        # Should find Charlie (friend of friend)
        assert len(results) >= 1

    async def test_relate_unsaved_model(self) -> None:
        """Relating an unsaved model raises error."""
        unsaved = Person(name="Nobody")
        with pytest.raises(SurrealDbError, match="unsaved model"):
            await unsaved.relate("follows", "Person:alice")

    async def test_fetch_clause_e2e(self) -> None:
        """FETCH clause resolves record links."""
        # Use raw query to verify FETCH generates valid SurrealQL
        client = await surreal_orm_lite.SurrealDBConnectionManager.get_client()

        # Create a post-like structure using raw queries
        with contextlib.suppress(NotFoundError):
            await client.query("DELETE Post;", {})
        await client.query(
            "CREATE Post:1 SET title = 'Hello', author = Person:alice;",
            {},
        )

        # Verify FETCH works by executing raw query with FETCH
        results = await client.query(
            "SELECT * FROM Post FETCH author;",
            {},
        )
        assert len(results) > 0
        post = results[0]
        # With FETCH, author should be resolved to the full record
        assert isinstance(post["author"], dict)
        assert post["author"]["name"] == "Alice"

        # Clean up
        with contextlib.suppress(NotFoundError):
            await client.query("DELETE Post;", {})

    async def test_remove_relation_missing_is_noop(self) -> None:
        alice = Person(id="rm_alice", name="Alice", age=30)
        await alice.save()
        # 'never_edge' relation table does not exist -> must be a silent no-op.
        await alice.remove_relation("never_edge", "Person:rm_bob")
        await alice.remove_all_relations("never_edge", direction="both")
        await alice.delete()

    async def test_remove_all_relations_both(self) -> None:
        """Remove all relations in both directions."""
        client = await surreal_orm_lite.SurrealDBConnectionManager.get_client()
        with contextlib.suppress(NotFoundError):
            await client.query("DELETE follows;", {})

        alice = await Person.objects().get("alice")
        bob = await Person.objects().get("bob")

        await alice.relate("follows", bob)
        await bob.relate("follows", alice)

        # Remove all 'follows' relations involving alice (both directions)
        await alice.remove_all_relations("follows", direction="both")

        # Alice should have no outgoing or incoming follows
        following = await alice.get_related("follows", direction="out", model_class=Person)
        followers = await alice.get_related("follows", direction="in", model_class=Person)
        assert len(following) == 0
        assert len(followers) == 0
