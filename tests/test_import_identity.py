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

The last test guards the import on the lowest supported Python: an annotation that only Python
3.14 and later accept (they evaluate annotations lazily) breaks the import on 3.11 to 3.13, while
a suite run on a newer interpreter stays green.
"""

import ast
import importlib
import re
import sys
from collections.abc import Iterator
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


def _eagerly_evaluated(tree: ast.Module) -> Iterator[ast.expr]:
    """The annotations of ``tree`` that Python 3.11 to 3.13 evaluate when its code runs.

    Without ``from __future__ import annotations``: those of every ``def``, nested ones included
    (evaluated each time the enclosing function runs), and the ``name: annotation`` of the module
    and class bodies. One inside a function body is never evaluated.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            arguments = node.args
            for argument in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
                arguments.vararg,
                arguments.kwarg,
            ):
                if argument is not None and argument.annotation is not None:
                    yield argument.annotation
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.Module | ast.ClassDef):
            yield from (statement.annotation for statement in node.body if isinstance(statement, ast.AnnAssign))


def _postpones_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def test_every_annotation_evaluates_as_on_python_3_11() -> None:
    """Python 3.11 to 3.13 evaluate an annotation when its ``def`` runs; 3.14 defers it (PEP 649).

    So ``tuple[Alias | None, int]``, with ``Alias`` a string type alias, imports on 3.14 and
    raises ``TypeError`` on 3.11 (#261). The suite may run on a newer interpreter than CI's lowest
    one, so this evaluates each such annotation of a module that does not postpone annotations in
    that module's namespace, as 3.11 does. A name it cannot resolve (a local of the enclosing
    function) is left to the 3.11 run of the suite.
    """
    package = Path(surreal_orm_lite.__file__).resolve().parent
    failures = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _postpones_annotations(tree):
            continue
        parts = path.relative_to(package.parent).with_suffix("").parts
        module = importlib.import_module(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
        for annotation in _eagerly_evaluated(tree):
            code = compile(ast.Expression(annotation), str(path), "eval")
            try:
                eval(code, dict(vars(module)))
            except NameError:
                continue
            except Exception as error:  # any other error is what 3.11 raises at import or call time
                failures.append(f"{path.relative_to(package.parent)}:{annotation.lineno}: {error!r}")
    assert failures == [], (
        "these annotations fail on Python 3.11 to 3.13; add `from __future__ import annotations` "
        f"to the module or quote the whole annotation: {failures}"
    )
