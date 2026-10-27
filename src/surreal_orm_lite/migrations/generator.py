"""The writer of migration files (the full SurrealDB-ORM's ``migrations.generator``).

``MigrationGenerator(migrations_dir).generate(name, operations, dependencies)`` writes ``<NNNN>_<name>.py`` — the
directory's next number, the name as ``generate_migration_name()`` makes it — a Python module defining ``migration =
Migration(...)`` in the full ORM's layout; ``generate_empty_migration()`` writes one without operations, to fill in by
hand. The file is deterministic but for its ``Generated:`` line (the UTC time):

- each operation is a call of its class with its arguments as keywords, in dataclass field order; an argument whose
  absence gives the same operation is left out (its default, or what the operation derives from the others, such as a
  field's ``path`` from its name);
- a value is written as Python source: ``repr()`` of a ``str``, ``int``, ``float``, ``bool``, ``None``, ``Decimal``
  (``from decimal import Decimal``) or ``datetime`` (``import datetime``; a ``tzinfo`` that is not a
  ``datetime.timezone`` is written as UTC, the same instant), a list for a sequence — a tuple where the operation keeps
  one (an index's ``bm25`` pair, a tuple default) —, a dict for a mapping, a nested operation (``previous=``,
  ``state_operations=``) as its own call, ``SurrealFunc('…')`` (``from surreal_orm_lite import SurrealFunc``) and
  ``DefaultExpression('…')``; a ``str``, ``int`` or ``float`` subclass (an enum member) as its base value;
- each import is written only when used; a call, a list or a dict longer than 120 columns is wrapped one item per line
  (the full ORM's 60-character rule, extended).

Text from the models or the database reaches the file as escaped data only (``repr()`` of a ``str``). Nothing is
written for an operation the file cannot hold — a value of another type, a function, a non-finite float, an operation
class of another module, or one that would not load back equal —: ``generate()`` raises ``TypeError`` before the file
exists. The file is created exclusively: an existing one is never replaced (``FileExistsError``).
"""

import dataclasses
import math
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from ..functions import SurrealFunc
from . import operations as _operations
from .migration import Migration, generate_migration_name, parse_migration_name
from .operations import DefaultExpression, Operation

__all__ = ["MigrationGenerator", "generate_empty_migration"]

#: The column a call, a list or a dict stays within when it can be wrapped.
_WIDTH = 120
#: One level of indentation.
_INDENT = 4
#: The ``__init__.py`` ``ensure_directory()`` writes (the full ORM's).
_PACKAGE = '"""Auto-generated migrations package."""\n'

_IMPORT_DATETIME = "import datetime"
_IMPORT_DECIMAL = "from decimal import Decimal"
_IMPORT_SURREAL_FUNC = "from surreal_orm_lite import SurrealFunc"


@dataclasses.dataclass(frozen=True)
class _Group:
    """A call, a list, a tuple or a dict: *opening*, its items (each a prefix — ``name=``, ``'key': `` or nothing —
    and a value) and *closing*."""

    opening: str
    items: tuple[tuple[str, "_Source"], ...]
    closing: str
    tuple_: bool = False


#: A value's Python source: a text, or a group that can be wrapped.
_Source = str | _Group


def _flat(source: _Source) -> str:
    """*source* on one line."""
    if isinstance(source, str):
        return source
    inner = ", ".join(prefix + _flat(value) for prefix, value in source.items)
    comma = "," if source.tuple_ and len(source.items) == 1 else ""
    return f"{source.opening}{inner}{comma}{source.closing}"


def _layout(source: _Source, *, indent: int, column: int, trailer: int) -> str:
    """*source* starting at *column* of a line indented by *indent* and followed by *trailer* characters: on one line
    when it fits within :data:`_WIDTH`, else one item per line, each indented one level more and ending with ``,``,
    and the closing bracket back at *indent*."""
    flat = _flat(source)
    if isinstance(source, str) or not source.items or column + len(flat) + trailer <= _WIDTH:
        return flat
    inner = indent + _INDENT
    lines = [source.opening]
    for prefix, value in source.items:
        text = _layout(value, indent=inner, column=inner + len(prefix), trailer=1)
        lines.append(f"{' ' * inner}{prefix}{text},")
    lines.append(f"{' ' * indent}{source.closing}")
    return "\n".join(lines)


def _plain(value: Any) -> Any:
    """*value* as the file gives it back: a mapping as a ``dict``, a list and a tuple as such, a ``str``, ``int`` or
    ``float`` subclass (an enum member) as its base value, a ``datetime`` as a ``datetime`` whose ``tzinfo`` is
    ``None`` or a ``datetime.timezone`` (another ``tzinfo`` gives the same instant in UTC); anything else as it is."""
    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None and type(value.tzinfo) is not timezone:
            value = value.astimezone(UTC)
        if type(value) is not datetime:
            value = datetime(
                value.year,
                value.month,
                value.day,
                value.hour,
                value.minute,
                value.second,
                value.microsecond,
                value.tzinfo,
                fold=value.fold,
            )
        return value
    if isinstance(value, Mapping):
        return {_plain(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_plain(item) for item in value)
    return value


def _holds_tuple(value: Any) -> bool:
    """Whether *value* (a :func:`_plain` value) is a tuple or holds one."""
    if isinstance(value, tuple):
        return True
    if isinstance(value, list):
        return any(_holds_tuple(item) for item in value)
    if isinstance(value, dict):
        return any(_holds_tuple(item) for item in value.values())
    return False


def _listed(value: Any) -> Any:
    """*value* (a :func:`_plain` value) with every tuple a list."""
    if isinstance(value, list | tuple):
        return [_listed(item) for item in value]
    if isinstance(value, dict):
        return {key: _listed(item) for key, item in value.items()}
    return value


def _rebuilt(kind: type[Operation], arguments: Mapping[str, Any]) -> Operation | None:
    """The operation of class *kind* built from *arguments*, or ``None`` when it refuses them."""
    try:
        return cast(Callable[..., Operation], kind)(**arguments)
    except (TypeError, ValueError):
        return None


class _Writer:
    """The source of one file's operations, and the imports and operation classes it uses."""

    def __init__(self) -> None:
        self.imports: set[str] = set()
        self.classes: set[str] = set()

    def operation(self, operation: Operation, *, subject: str) -> _Group:
        """*operation* as a call of its class, *subject* naming it in a refusal."""
        kind = type(operation)
        if kind.__name__ not in _operations.__all__ or getattr(_operations, kind.__name__) is not kind:
            raise TypeError(
                f"MigrationGenerator: {subject} is a {kind.__name__}, not an operation of "
                "surreal_orm_lite.migrations.operations; a migration file writes those only."
            )
        arguments = self._arguments(operation)
        items = tuple(
            (f"{name}=", self.value(value, subject=subject, where=f"{kind.__name__}.{name}="))
            for name, value in arguments.items()
        )
        # Last: a value the file cannot write is refused by its name first (a NaN never equals itself).
        if _rebuilt(kind, arguments) != operation:
            raise TypeError(
                f"MigrationGenerator: {subject} does not load back equal from its arguments, so a migration file "
                "cannot write it."
            )
        self.classes.add(kind.__name__)
        return _Group(f"{kind.__name__}(", items, ")")

    @staticmethod
    def _arguments(operation: Operation) -> dict[str, Any]:
        """The keyword arguments the file writes for *operation*, in dataclass field order: each value as the file
        gives it back, a tuple as a list where the operation takes the list back to the same tuple, and every argument
        whose absence gives the same operation left out. :meth:`operation` checks that they build it back."""
        kind = type(operation)
        fields = dataclasses.fields(cast(Any, operation))
        arguments = {field.name: _plain(getattr(operation, field.name)) for field in fields}
        for name, value in arguments.items():
            if _holds_tuple(value):
                listed = {**arguments, name: _listed(value)}
                if _rebuilt(kind, listed) == operation:
                    arguments = listed
        for field in fields:
            if field.default is dataclasses.MISSING:  # required (no operation argument has a default factory)
                continue
            value = arguments[field.name]
            without = {name: item for name, item in arguments.items() if name != field.name}
            if (type(value) is type(field.default) and value == field.default) or _rebuilt(kind, without) == operation:
                arguments = without
        return arguments

    def value(self, value: Any, *, subject: str, where: str) -> _Source:
        """The source of *value* (a :func:`_plain` value), with the imports it needs; *where* names the argument in a
        refusal."""
        if value is None or isinstance(value, bool | int | str):
            return repr(value)
        if isinstance(value, float):
            if not math.isfinite(value):
                raise self._unwritable(subject, where, "a non-finite float")
            return repr(value)
        if isinstance(value, Decimal):
            self.imports.add(_IMPORT_DECIMAL)
            return repr(value)
        if isinstance(value, datetime):
            self.imports.add(_IMPORT_DATETIME)
            return repr(value)
        if type(value) is SurrealFunc:
            self.imports.add(_IMPORT_SURREAL_FUNC)
            return f"SurrealFunc({value.expression!r})"
        if type(value) is DefaultExpression:
            self.classes.add(DefaultExpression.__name__)
            return f"DefaultExpression({value.text!r})"
        if isinstance(value, Operation):
            return self.operation(value, subject=subject)
        if isinstance(value, list | tuple):
            items = tuple(("", self.value(item, subject=subject, where=where)) for item in value)
            return _Group("[", items, "]") if isinstance(value, list) else _Group("(", items, ")", tuple_=True)
        if isinstance(value, dict):
            entries = tuple(
                (f"{_flat(self.value(key, subject=subject, where=where))}: ", self.value(item, subject=subject, where=where))
                for key, item in value.items()
            )
            return _Group("{", entries, "}")
        raise self._unwritable(subject, where, f"a {type(value).__name__}")

    @staticmethod
    def _unwritable(subject: str, where: str, what: str) -> TypeError:
        return TypeError(f"MigrationGenerator: {subject}: {where} holds {what}, which a migration file cannot write.")


def _render(migration: Migration) -> str:
    """The text of *migration*'s file.

    :raises TypeError: an operation the file cannot hold.
    """
    writer = _Writer()
    calls = [
        writer.operation(operation, subject=f"operation {position} ({operation.describe()})")
        for position, operation in enumerate(migration.operations, 1)
    ]
    lines = [
        '"""',
        f"Migration: {migration.name}",
        f"Generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
        "Auto-generated migration file. Review before applying.",
        '"""',
        "",
    ]
    standard = [line for line in (_IMPORT_DATETIME, _IMPORT_DECIMAL) if line in writer.imports]
    if standard:
        lines += [*standard, ""]
    if _IMPORT_SURREAL_FUNC in writer.imports:
        lines.append(_IMPORT_SURREAL_FUNC)
    lines.append("from surreal_orm_lite.migrations import Migration")
    if writer.classes:
        lines += [
            "from surreal_orm_lite.migrations.operations import (",
            *(f"    {name}," for name in sorted(writer.classes)),
            ")",
        ]
    prefix = "    dependencies="
    dependencies = _Group("[", tuple(("", repr(name)) for name in migration.dependencies), "]")
    lines += [
        "",
        "",
        "migration = Migration(",
        f'    name="{migration.name}",',
        f"{prefix}{_layout(dependencies, indent=_INDENT, column=len(prefix), trailer=1)},",
    ]
    if calls:
        indent = 2 * _INDENT
        lines.append("    operations=[")
        lines += [f"{' ' * indent}{_layout(call, indent=indent, column=indent, trailer=1)}," for call in calls]
        lines.append("    ],")
    else:
        lines.append("    operations=[],")
    lines += [")", ""]
    return "\n".join(lines)


class MigrationGenerator:
    """Writes migration files in a migrations directory (the full ORM's class).

    ``generate()`` writes ``<NNNN>_<name>.py`` — see the module's documentation for its text —, numbered after the
    highest number of the directory's migration files; the file can be reviewed and edited before it is applied.

    Attributes:
        migrations_dir: The migrations directory.
    """

    def __init__(self, migrations_dir: str | os.PathLike[str]) -> None:
        self.migrations_dir = Path(migrations_dir)

    def ensure_directory(self) -> None:
        """Create the directory (and its parents) when missing, and its ``__init__.py`` when missing; an existing
        ``__init__.py`` is kept."""
        self.migrations_dir.mkdir(parents=True, exist_ok=True)
        try:
            with (self.migrations_dir / "__init__.py").open("x", encoding="utf-8", newline="\n") as package:
                package.write(_PACKAGE)
        except FileExistsError:
            pass

    def get_next_number(self) -> int:
        """The highest number of the directory's migration files plus one — the ``*.py`` files whose name does not
        start with ``_`` and that ``parse_migration_name()`` reads —; ``1`` for an empty or missing directory, which is
        not created."""
        highest = 0
        for path in self.migrations_dir.glob("*.py"):
            if path.name.startswith("_"):
                continue
            try:
                number, _ = parse_migration_name(path.name)
            except ValueError:
                continue
            highest = max(highest, number)
        return highest + 1

    def generate(self, name: str, operations: Sequence[Operation], dependencies: Sequence[str] | None = None) -> Path:
        """Write the migration of *operations* (in order), depending on *dependencies*, and return its path.

        *name* is free text: the file's name is ``generate_migration_name(<next number>, name)`` (``"Add Email"`` →
        ``0002_add_email.py``). The migration is checked as ``Migration`` checks it and its text built before anything
        is written; the directory and its ``__init__.py`` are then created when missing, and the file exclusively.

        Raises:
            TypeError: *name* is not a ``str``; an argument ``Migration`` refuses; an operation the file cannot hold
                (a value of another type, a function, a non-finite float, an operation class of another module, an
                operation that would not load back equal). Nothing is written.
            ValueError: *name* holds no letter or digit; a dependency ``Migration`` refuses. Nothing is written.
            FileExistsError: The file exists (another writer took the number); it is left as it is.
        """
        if not isinstance(name, str):
            raise TypeError(f"MigrationGenerator: name must be a str, not {type(name).__name__}.")
        full_name = generate_migration_name(self.get_next_number(), name)
        migration = Migration(name=full_name, dependencies=() if dependencies is None else dependencies, operations=operations)
        text = _render(migration)
        self.ensure_directory()
        path = self.migrations_dir / f"{full_name}.py"
        with path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(text)
        return path


def generate_empty_migration(
    migrations_dir: str | os.PathLike[str], name: str, dependencies: Sequence[str] | None = None
) -> Path:
    """Write a migration without operations, depending on *dependencies*, in *migrations_dir*, and return its path —
    a file to fill in by hand (a ``RawSQL``, a ``DataMigration``). ``MigrationGenerator(migrations_dir).generate(name,
    [], dependencies)``, with its refusals."""
    return MigrationGenerator(migrations_dir).generate(name, [], dependencies)
