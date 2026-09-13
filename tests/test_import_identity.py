"""The package must be imported under exactly one name.

This is a structural guard, not a behaviour test. The repo is a src layout, so
``src.surreal_orm_lite`` resolved alongside ``surreal_orm_lite`` and the suite imported both.
Python builds a separate module object per spelling, so the suite ran against two
``SurrealDBConnectionManager`` classes (each with its own client cache), two signal registries
and two alias caches — making ``conftest``'s autouse cleanup a no-op for half the tests, and
letting a signal handler registered under one spelling silently never fire for a model defined
under the other.

Both failure modes are invisible in a passing run, which is why they survived so long. These
tests make the split loud instead.
"""

import re
import sys
from pathlib import Path

import surreal_orm_lite
from surreal_orm_lite import SurrealDBConnectionManager
from tests.conftest import orm_client


def test_the_package_is_loaded_under_one_name() -> None:
    duplicates = sorted(name for name in sys.modules if name.startswith("src.surreal_orm_lite") or name == "src")
    assert duplicates == [], (
        f"the package is loaded twice, under {duplicates} as well as surreal_orm_lite; "
        "import it as `surreal_orm_lite` everywhere (see tests/conftest.py)"
    )


def test_every_submodule_is_loaded_once() -> None:
    """A duplicate would show up as two module objects sharing one ``__file__``."""
    by_file: dict[str, list[str]] = {}
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if path and "surreal_orm_lite" in path:
            by_file.setdefault(path, []).append(name)
    doubled = {path: names for path, names in by_file.items() if len(names) > 1}
    assert doubled == {}, f"these files are loaded under more than one module name: {doubled}"


def test_conftest_shares_the_suite_s_connection_manager() -> None:
    """The autouse cleanup only works if it drops the cache the tests actually fill."""
    assert orm_client.__module__ == "tests.conftest"
    assert sys.modules["tests.conftest"].SurrealDBConnectionManager is SurrealDBConnectionManager
    assert surreal_orm_lite.SurrealDBConnectionManager is SurrealDBConnectionManager


def test_no_source_file_spells_the_package_the_other_way() -> None:
    """A static scan, because the runtime checks above only see what ran before them.

    A module-level ``from src.surreal_orm_lite import …`` is caught either way (test modules are
    imported during collection), but an import inside a function or a fixture would slip past
    them depending on ordering. Grepping the tree is order-independent.
    """
    banned = re.compile(r"\bfrom\s+src(\.surreal_orm_lite)?\s+import\b|\bimport\s+src\.surreal_orm_lite\b")
    root = Path(__file__).resolve().parent.parent
    offenders = [
        f"{path.relative_to(root)}:{number}"
        for path in sorted([*(root / "tests").rglob("*.py"), *(root / "src").rglob("*.py")])
        if path.name != Path(__file__).name
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if banned.search(line)
    ]
    assert offenders == [], f"import the package as `surreal_orm_lite`; found the `src.` spelling at {offenders}"
