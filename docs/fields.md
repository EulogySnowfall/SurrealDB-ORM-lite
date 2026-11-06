# Field types

Rich field types (datetimes, durations, decimals, sets, ranges…) and geospatial fields.

## 24. Rich field types

Since v0.23.0 a model can declare `timedelta`, `date` and `set[X]`/`frozenset[X]` fields, and the
ORM's own `Duration` and `Range[T]`: every write stores them as SurrealDB's native values and every
read returns the declared type, with the same behaviour on SurrealDB 2.x and 3.x (one exception:
`TYPE set<…>` columns, see [Sets](#sets)). `datetime`, `Decimal`, `UUID` and `bytes` fields keep
working as before.

```python
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from surreal_orm_lite import (
    BaseSurrealModel,
    BoundExcluded,
    BoundIncluded,
    Duration,
    Range,
    SurrealConfigDict,
)


class Subscription(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="subscription")

    id: str | None = None
    started_at: datetime
    renews_on: date
    trial: timedelta
    grace: Duration | None = None
    price: Decimal
    token: UUID
    active_hours: Range[int]
    period: Range[datetime] | None = None
    logo: bytes | None = None
    tags: set[str] = set()


await Subscription(
    id="acme",
    started_at=datetime(2026, 1, 2, 9, 30, tzinfo=UTC),
    renews_on=date(2026, 2, 1),
    trial=timedelta(days=14),
    grace=Duration.parse("1h30m1ns"),                         # nanoseconds kept
    price=Decimal("9.99"),
    token=UUID("0190c3d1-5c3b-7cc6-8f8a-2d2b1f0e9a11"),
    active_hours=Range(BoundIncluded(9), BoundExcluded(17)),  # 9..17: 9 included, 17 excluded
    period=Range(BoundIncluded(datetime(2026, 1, 1, tzinfo=UTC)), None),  # open end
    logo=b"\x89PNG",
    tags={"pro", "annual"},
).save()

sub = await Subscription.objects().get("acme")
sub.trial         # timedelta(days=14): a duration on the server
sub.renews_on     # date(2026, 2, 1): a datetime at midnight UTC on the server
sub.tags          # {'annual', 'pro'}: an array on the server
sub.active_hours  # Range(begin=BoundIncluded(value=9), end=BoundExcluded(value=17))
```

| Annotation                   | Sent as                                                                      | Stored as        | Read back as                                |
| ---------------------------- | ---------------------------------------------------------------------------- | ---------------- | ------------------------------------------- |
| `datetime`                   | as before (a naive value is taken as UTC)                                    | `datetime` (UTC) | an aware UTC `datetime`, to the microsecond |
| `Decimal`                    | as before; a value written with an exponent goes out in fixed-point notation | `decimal`        | `Decimal`                                   |
| `UUID`                       | as before                                                                    | `uuid`           | `UUID`                                      |
| `bytes`                      | as before                                                                    | `bytes`          | `bytes`                                     |
| `timedelta`                  | a duration                                                                   | `duration`       | `timedelta`, floored to the microsecond     |
| `date` (subclasses included) | a `datetime` at midnight UTC                                                 | `datetime`       | `date`                                      |
| `set[X]`, `frozenset[X]`     | an array, sorted when its items can be ordered                               | `array`          | the set or frozenset (see [Sets](#sets))    |
| `Duration`                   | a duration                                                                   | `duration`       | `Duration`, to the nanosecond               |
| `Range[T]`, `Range`          | a range; an open side as SurrealDB's null bound                              | `range`          | `Range`, each bound value validated as `T`  |

Optional fields (`ttl: timedelta | None = None`) behave the same. Values nested in containers
(`list[timedelta]`, `dict[str, date]`, a sub-model's fields) are sent the same way at any depth. On
reads, a `timedelta` is converted from the SDK's duration at any depth too: a top-level field,
`list[timedelta]`, `dict[str, timedelta]`, `timedelta | str` or a sub-model's `timedelta` field
all read a `timedelta`, floored to the microsecond (declare `Duration` to keep the nanoseconds).
Your validators (any mode) and `model_post_init` still apply once to the value read, even when they
change it in place. A naive `datetime` is written as UTC and read back as an aware UTC `datetime`.
The instance keeps it naive until a call applies the row the server returns (see
[After a write that returns the row](#after-a-write-that-returns-the-row)): after `save()` (since
#261 with an explicit id too), `refresh()`, `merge()` (default `refresh=True`), `upsert()`,
`patch()` or an `atomic_*` helper, the field holds that aware UTC `datetime`, so compare it with
aware values. `update()`, and `save(tx=)` in a buffered transaction, leave it naive.

### `Duration`

```python
from datetime import timedelta

import surrealdb
from surreal_orm_lite import Duration, InvalidDurationError

d = Duration.parse("1h30m1ns")
d.elapsed                 # 5400000000001: nanoseconds
d.to_string()             # '1h30m1ns': every unit, lossless (str(d) too)
d.to_compact()            # [5400, 1]: [seconds, nanoseconds]
d.get_seconds_and_nano()  # (5400, 1)
d.to_timedelta()          # timedelta(seconds=5400): floored to the microsecond
Duration.parse(90, 5)     # Duration(elapsed=90000000005): an int is seconds, then nanoseconds
Duration.from_timedelta(timedelta(days=7))            # Duration(elapsed=604800000000000)
Duration.parse(" 1h30m ") == Duration.parse("1h30m")  # True: surrounding whitespace is ignored

try:
    Duration.parse("1h 30m")             # "1.5h" is refused too
except InvalidDurationError as exc:      # a ValueError and a surrealdb.InvalidDurationError
    print(exc)

Duration(5) == surrealdb.Duration(5)     # True
surrealdb.Duration(5) == Duration(5)     # False: keep the ORM value on the left
```

`timedelta` stops at microseconds: declare a field `Duration` to keep the server's nanoseconds.
`Duration` is the ORM's own class, not the SDK's `surrealdb.Duration`, but it mirrors the SDK
2.0.0 class: the same members under the same names and parameters (`elapsed`,
`parse(value, nanoseconds=0)` with an `int` in seconds, `get_seconds_and_nano()`, `to_compact()`,
`to_string()` and the unit properties `seconds`, `minutes`, `hours`, `days`, …), with exact
results: `get_seconds_and_nano()` uses integer arithmetic, `to_compact()` keeps the nanoseconds
(SDK 2.0.0 returns `[seconds]`) and `to_string()` writes every unit (SDK 2.0.0 writes `"1h"` for
`1h30m1ns`). `parse(str)` reads the server's syntax strictly: surrounding whitespace is accepted,
`"1h 30m"` and `"1.5h"` are refused. A refused value raises `InvalidDurationError`, which is both a
`ValueError` and a `surrealdb.InvalidDurationError`; a wrong argument type raises `TypeError`.
`Duration` adds `MAX_NANOSECONDS` (the largest SurrealDB duration), `from_timedelta()`,
`to_timedelta()`, hashing and ordering.

As a field, `Duration` accepts a `Duration`, the SDK's `Duration`, a non-negative `timedelta` or a
SurrealQL duration string (`"1h30m"`), and refuses a bare number, which does not say its unit. In
JSON it is its SurrealQL text: `model_dump_json()` writes `"1h30m1ns"`, which
`model_validate_json()` reads back.

**Equality with the SDK's values is one-way.** `Duration(5) == surrealdb.Duration(5)` is `True`,
but `surrealdb.Duration(5) == Duration(5)` is `False`: the SDK's `__eq__` answers `False` for any
other type, so Python never asks the ORM's. The same holds for `Range` and the SDK's `Range`. Keep
the ORM value on the left, or compare `elapsed`.

### `Range[T]`

```python
from pydantic import TypeAdapter

from surreal_orm_lite import BoundExcluded, BoundIncluded, Range

str(Range(BoundIncluded(9), BoundExcluded(17)))  # '9..17'
str(Range(BoundExcluded(1), BoundIncluded(5)))   # '1>..=5'
str(Range(begin=BoundIncluded(65)))              # '65..': None is an open side
str(Range())                                     # '..'

hours = TypeAdapter(Range[int])
hours.validate_python(range(9, 17))  # Range(begin=BoundIncluded(value=9), end=BoundExcluded(value=17))
hours.validate_json('"9..17"')       # the same range: in JSON a Range is its SurrealQL text
```

A `Range` has two bounds, each `BoundIncluded(value)` or `BoundExcluded(value)` (the SDK's
classes, exported by the ORM) or `None` for an open side; `Range(1, 5)` raises `TypeError`. The
SDK's own spellings of an open side, a bare `Bound()`, `surrealdb.cbor.undefined` and a CBOR null,
are accepted too and stored as `None`. Whatever its spelling, an open side is sent as SurrealDB's
null bound, the form its protocol defines.

As a field, `Range[int]`, `Range[datetime]`, `Range[timedelta]`… validate each bound value as `T`
(a bare `Range` keeps them as given) and accept the SDK's `Range`, a builtin `range` with step 1
(`range(9, 17)` is `9..17`) or the SurrealQL text `str()` writes (`"9..17"`,
`"d'2026-01-01T00:00:00Z'.."`, `"1m..1h"`). A `Range[timedelta]` turns the SDK's duration bounds
into `timedelta`s (floored to the microsecond) and a `Range[date]` accepts midnight `datetime`
bounds. In JSON a `Range` is that text, which `model_validate_json()` reads back: every bound is
written exactly, a zero duration as `0ns` and a `Decimal` with all its digits, even where a live
query filter refuses the value. A `Range` is not hashable, like the SDK's.

### Filters

```python
class Slot(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="slot")

    id: str | None = None
    hour: int


for hour in (8, 9, 12, 17):
    await Slot(id=f"h{hour}", hour=hour).save()

# in / not_in with a Range: bound as one value (hour IN $_f0), never expanded
working = await Slot.objects().filter(hour__in=Range(BoundIncluded(9), BoundExcluded(17))).exec()  # 9, 12
late = await Slot.objects().filter(hour__not_in=Range(end=BoundExcluded(17))).exec()              # 17

# Filter values are sent like field values
trials = await Subscription.objects().filter(trial__gte=timedelta(days=7), renews_on=date(2026, 2, 1)).exec()
cheap = await Subscription.objects().filter(price__lte=Decimal("9.99"), tags__containsall={"pro"}).exec()
open_at_noon = await Subscription.objects().filter(active_hours__contains=12).exec()  # 12 in 9..17
graced = await Subscription.objects().filter(grace__lte=Duration.parse("2h")).exec()
```

Filter values on rich fields **must be typed**: they are sent exactly like field values, and
strings are not converted. `filter(trial__gte="7d")` compares the stored duration with a string,
which does not give the result you want: pass `timedelta(days=7)`.

`in` and `not_in` take a `Range`, the ORM's or the SDK's, and bind it as one value; a builtin
`range` keeps its meaning, the list of its integers. On the record-id column `id` and on an edge's
`in`/`out` columns a `Range` raises `ValueError`, since a record id never falls inside a value
range. `contains` on a range column tests membership (`active_hours__contains=12`). Live queries
([section 21](realtime.md#21-live-queries-real-time-notifications)) write these values into the statement as
SurrealQL literals (`1w`, `d'2026-02-01T00:00:00Z'`, `['pro']`, `9..17`), with the same results on
both lines.

### Precision and `Decimal`

- `datetime`: to the nanosecond (since v0.50.0). A datetime read from the server is the SDK's
  `PreciseDatetime`, a `datetime` subclass that keeps the server's nanoseconds in `.nanosecond`
  (the part below the microsecond): a stored `d'2026-01-02T03:04:05.123456789Z'` reads
  `…05.123456` with `.nanosecond == 789`, and `save()`, `update()`, `upsert()` and `merge()` write
  it back unchanged, on every line and the embedded engine. It compares equal to the `datetime` of
  the same microsecond. A `datetime` you build in Python is sent to the microsecond, as before.
  A field validator may convert it: `value.astimezone(UTC)` on a UTC value keeps the nanoseconds,
  while `astimezone()` to another zone and `replace()` return a value to the microsecond
  (`.nanosecond == 0`); the model accepts either. `update()` and `upsert()` rewrite the whole
  record, but a top-level `datetime` that still floors to the stored value keeps the stored
  nanoseconds, so such a validator does not change what is stored
  ([#261](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/261), up to 32 top-level
  datetimes per record). A datetime you changed is written as you set it, and a `READONLY` field
  still refuses it. Before v0.50.0 the SDK floored datetimes to the microsecond, so a `READONLY`
  datetime below the top level (in a sub-model, a `list`, a `tuple` or a set) could make
  `update()` fail; a value read from the server is now written back as it was read.
  SurrealDB 2.x also refuses, for now, `update()` and `upsert()` of an existing record that has
  a field with a non-optional `TYPE` and a `VALUE` clause the model does not write (a `Computed`
  field, such as a generated `updated: Computed[datetime]`): change it with `merge()` there
  ([#267](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/267)).
- `timedelta`: floored to the microsecond on read (`1h30m1ns` reads `timedelta(seconds=5400)`).
- `Duration`: exact, to the nanosecond.
- `Decimal`: a value whose `str()` writes no exponent is sent as before. A value written with an
  exponent (`Decimal("1E-7")`, `Decimal("1E+3")`) goes out in fixed-point notation (`0.0000001`,
  `1000`), cut after the 29th fractional digit: the server keeps at most 28 fractional digits,
  rounding half away from zero as before, so the stored value is the same. A finite value whose
  integer part, rounded, exceeds `2**96 - 1` (the largest SurrealDB decimal) is refused before
  sending. On reads, SurrealDB 2.x keeps trailing zeros (`Decimal("12.50")` reads `12.50`) where
  3.x normalises them (`12.5`); the values are equal. `NaN` and `±Infinity` are stored as floats,
  as before: declare `Annotated[Decimal, Field(allow_inf_nan=True)]` to read them back.

### Sets

`set[X]`/`frozenset[X]` fields are stored as arrays. On SurrealDB 3.3 and 4.x a `TYPE set<…>`
column stores the array as a native set, which the field reads back as the declared set; a raw
result (`raw_query()`, `QuerySet.query()`, a live or change-feed event's `raw`) holds it as a
Python `set` (a `list` when an item is unhashable, or when two items are equal in Python, such as
`1` and `True`). SurrealDB 2.x has no native set: the column is an array there, read as a `list`.
SurrealDB 3.1 refuses an array in a `set<…>` column; to support 3.1, declare such columns
`array<…>`, optionally with `VALUE $value.distinct()`:

```sql
DEFINE FIELD tags ON subscription TYPE array<string> VALUE $value.distinct();
```

On a schemaless or `array<…>` column the field reads back as the declared `set` or `frozenset`, on
both lines. The array is sorted when its items can be ordered, so a set is written the same way
every time.

### `NULL` and open range sides

SurrealDB's `NULL` reads as `None` on every read path (a model, `raw_query()`, `values()`, a live or
change-feed event), as `NONE` does: the SDK reads it as its `Null` sentinel and the ORM maps it.
A range read from the server holds `None` on an open side, in a `Range` field and in a raw result,
and binds back as a parameter as it is: the ORM sends an open side (`None`, or the SDK's `Null`)
as the protocol's open bound.

### Values refused before sending

Every value the ORM sends (a field, a filter value, a `call_function()` argument, an auth
variable) goes through the same check. A value SurrealDB would reject is refused before anything
is sent, with a plain `ValueError` that says why, never re-wrapped in a `SurrealDbError`:

| Value                                                                                     | Raised                       |
| ----------------------------------------------------------------------------------------- | ---------------------------- |
| A negative duration (a `timedelta`, the SDK's `Duration`)                                 | `ValueError`                 |
| A duration of `2**64` seconds or more                                                     | `ValueError`                 |
| A finite `Decimal` whose integer part, rounded, exceeds `2**96 - 1`                       | `ValueError`                 |
| An `int` outside the signed 64-bit range                                                  | `ValueError` (use `Decimal`) |
| A `Range` side that is neither `BoundIncluded`/`BoundExcluded` nor an open side           | `ValueError`                 |
| `id__in=Range(…)`, `id__not_in=Range(…)`, or the same on an edge's `in`/`out`             | `ValueError`                 |
| `atomic_increment(field, <a duration>)`                                                   | `TypeError` (use `merge()`)  |
| In a live filter, a negative or oversized duration, or a range bound with no literal form | `TypeError` from `live()`    |
| A `Duration` or `Range` field given a value it does not accept                            | Pydantic's `ValidationError` |

A zero duration (`timedelta(0)`, `Duration(0)`) is written, filtered and read back like any
other, on every line, in a live filter too (`0ns`). Filter
"longer than zero" with `__gt=Duration(0)` or `__gte=Duration(1)`. `atomic_increment()` adds
numbers only: SurrealDB 2.x ignores `+=` with a duration, so the ORM refuses it on both lines; set
the new value with `merge()`:

```python
nonzero = await Subscription.objects().filter(trial__gte=Duration(1)).exec()  # longer than zero
await sub.merge(trial=sub.trial + timedelta(days=7))                          # not atomic_increment()
```

### After a write that returns the row

`save()` (with or without an id, an edge's too), `refresh()`, `merge()`, `upsert()`, `patch()` and
the `atomic_*` helpers apply the row the server returns to the instance. After `save()` the
instance therefore holds what the record holds: the `server_fields` and computed fields, the
sub-fields a `DEFAULT` filled (`prefs.at DEFAULT time::now()`), and the values the server coerced
or rewrote (`VALUE string::lowercase($value)`), so a following `update()` writes them back
unchanged ([#261](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/261)). An id you
gave stays as you gave it; an auto id is the server's `RecordID`. A buffered transaction (HTTP,
SurrealDB 2.x, the embedded engine) returns no row before commit: there `save(tx=)` leaves the
instance as it was, and `refresh()` after the commit reads the row.

The top-level `timedelta`, `Duration` and geometry fields (since v0.24.0, see
[section 25](#25-geospatial-fields)) are converted on the way, and every other field gets the
value a read gives (#261; before, only a field holding a sub-model, a geometry or a nested
`timedelta` did): the row goes through the model's validation, the model's own validators
included. A sub-model field holds its model instance, not the row's `dict`, with the ORM's
durations, `timedelta`s, ranges and geometries inside; an enum field holds the enum, not its `str`
or `int`; a `tuple`, set, `date` or `Range` field holds its declared type, not the SDK's `list`,
midnight `datetime` or `Range`; a `float` field stored in a `decimal` column holds a `float`, not a
`Decimal`; a `list[Duration]` holds the ORM's durations. When the row as a whole does not validate,
each field is validated on its own, except one that the model's own validators could reshape (a
`field_validator` on that field, a `model_validator`, a `model_post_init`): that one, like a value
that does not validate, is applied as received, so these calls never fail on it. The `id` keeps
the server's `RecordID`. A column the model does not declare goes where a read puts it: as the SDK
reads it into `model_extra` under `extra="allow"`, nowhere otherwise. So a column holding a Pydantic
`@computed_field` (`model_dump()` writes it) or named like a method (`json`) never replaces that
attribute.

Writing such an instance back is safe, sub-models included: a following `update()` replaces the
record with these values, as after `get()`. So a `float` field stored in a `decimal` column writes
the float's rounding (`0.12345678901234567890` becomes `0.1234567890123457`: declare `Decimal` to
keep every digit), and under `extra="allow"` the columns in `model_extra` are written back, not
removed. An enum declared `(str, Enum)` or `(int, Enum)` holds the member, as after `get()`: it
compares equal to its value, but `str()` and f-strings give `Color.RED`, not `red` (a `StrEnum` or
an `IntEnum` gives the value), so use `.value` to build a key, a URL or a log line from it.

### Values the SDK cannot read

The surrealdb SDK refuses two kinds of values SurrealDB stores and returns: a degenerate geometry
(see [Geometries the SDK cannot read](#geometries-the-sdk-cannot-read)) and a datetime outside
years 1-9999, the years a Python `datetime` holds (SurrealDB stores years -262143 to +262142, on
every line). The clients the ORM opens read them as in 0.40.x, through the SDK's own CBOR decoder
with these two values handled; everything else reads exactly as the SDK reads it, a zero duration
and a native set included (see [Sets](#sets)):

- a degenerate geometry reads as its GeoJSON `dict`;
- a datetime before `0001-01-01T00:00:00Z` or after `9999-12-31T23:59:59.999999999Z` reads as its
  RFC 3339 text, the one SurrealDB gives it (`<string>`): `"0000-06-01T00:00:00Z"`,
  `"-0001-01-01T00:00:00Z"`, `"+10000-01-01T00:00:00.500Z"` (a sign and at least four digits outside
  years 0000-9999, every nanosecond kept). An untyped field, `raw_query()` and live notifications
  give that string; a `datetime` field refuses it with a `ValidationError` naming the field: `get()`
  raises it, and `filter().exec()` and `raw_query()` return such rows as dicts, as for any row that
  does not validate. A datetime within years 1-9999, before 1970 included, reads as the SDK's
  `datetime`, as before.

Written back through the ORM (`merge(v=row.v)`, or `update()`, which rewrites the whole row, the
untouched fields included), a degenerate geometry is stored as a plain object (see
[Geometries the SDK cannot read](#geometries-the-sdk-cannot-read)), and a datetime outside years
1-9999 as its text, a string.

The hook is installed only while the installed SDK cannot decode these values, and only on the
ORM's own clients (`SurrealDBConnectionManager.get_client()`, `ephemeral_client()`), the embedded
engine's included (see [Embedded engine and test fixtures](embedded.md#26-embedded-engine-and-test-fixtures)).
An `AsyncSurreal` client the application builds itself keeps the SDK's behaviour: an undecodable
value raises the SDK's `UnexpectedResponseError` there, and through the ORM an undecodable value
raises `SurrealDbConnectionError`.

### Authentication

Rich values in the fields of an `AuthenticatedUserMixin` model's `signup()`, its defaults included
(`trial: timedelta = timedelta(days=7)`), and in the `variables=` of
`SurrealDBConnectionManager.signin()`/`signup()` are sent like any other write: a `date` reaches
the `SIGNUP` clause as a midnight UTC `datetime` and a `timedelta` as a duration, on both lines. A
refused value raises `ValueError` before anything is sent.

### Coming from the full SurrealDB-ORM

Plain `datetime`, `Decimal` and `UUID` annotations work as they do there. The full ORM has no
`timedelta` or `Range` fields and its `Duration` is a string: `timedelta`, `Duration` and
`Range[T]` fields are lite additions. The full ORM also validates the rows its writes return; so
does lite, every field but the `id` since #261, with top-level durations converted (see
[After a write that returns the row](#after-a-write-that-returns-the-row)).

## 25. Geospatial fields

Since v0.24.0 a model can declare geometry fields: every write stores them as SurrealDB's native
geometries, every read returns the ORM's geometry classes, and `nearby()`, `GeoDistance` and the
`inside`, `outside` and `intersects` lookups query them, with the same results and the same
distances on SurrealDB 2.x and 3.x. The names are the full SurrealDB-ORM's (`GeoField`,
`PointField`, `nearby()`, `GeoDistance`); what differs is listed at the end of this section.

### Declaring geometry fields

```python
from surreal_orm_lite import (
    BaseSurrealModel,
    Computed,
    GeoField,
    GeometryPoint,
    LineStringField,
    PointField,
    PolygonField,
    SurrealConfigDict,
    computed,
)


class Restaurant(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="restaurant")

    id: str | None = None
    name: str
    location: PointField = None                # GeometryPoint or None, default None: the full ORM's form
    delivery_area: GeoField["polygon"] = None  # the same form, with a kind name (see Type checkers)
    entrance: GeometryPoint | None = None      # a class annotation (required without "| None = None")


class Zone(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="zone")

    id: str | None = None
    name: str
    area: PolygonField = None
    centroid: Computed[GeometryPoint] = computed("IF area.is_polygon() THEN geo::centroid(area) END")


class Route(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="route")

    id: str | None = None
    name: str
    path: LineStringField = None
    stops: list[GeometryPoint] = []  # inside containers: the classes, not the aliases


await Zone.define_computed_fields()  # the centroid's DEFINE FIELD (see Computed geometry fields)
```

| Annotation                | Holds                      | `GeoField` kind                            |
| ------------------------- | -------------------------- | ------------------------------------------ |
| `PointField`              | `GeometryPoint`            | `"point"`                                  |
| `LineStringField`         | `GeometryLine`             | `"line"` (or `"linestring"`)               |
| `PolygonField`            | `GeometryPolygon`          | `"polygon"`                                |
| `MultiPointField`         | `GeometryMultiPoint`       | `"multipoint"`                             |
| `MultiLineStringField`    | `GeometryMultiLine`        | `"multiline"` (or `"multilinestring"`)     |
| `MultiPolygonField`       | `GeometryMultiPolygon`     | `"multipolygon"`                           |
| `GeometryCollectionField` | `GeometryCollection`       | `"collection"` (or `"geometrycollection"`) |
| `GeometryField`           | any of them (a `Geometry`) | `"feature"` (or `"geometry"`)              |

`GeoField["kind"]` and the aliases are nullable with default `None`, as in the full ORM: the field
is not required and `model_construct()` sets `None`. The kind is one case-insensitive string;
several kinds are separated by a pipe (`GeoField["point|polygon"]` holds a point or a polygon), and
an unknown kind raises `ValueError` where the class is defined. A class annotation
(`entrance: GeometryPoint`) is required unless written `GeometryPoint | None = None`, and
`Geometry` holds any kind. At nested positions (`list[GeometryPoint]`, `dict[str, GeometryPolygon]`,
a sub-model's field) use the classes: an alias inside a container validates normally, but Pydantic
warns at class definition that its default has no effect there. Geometry fields work on edge
models (`BaseSurrealEdge`) and with field aliases (`Field(default=None, alias="loc")`).
`is_geo_field()` and `get_geo_info()`, from `surreal_orm_lite.geo`, tell whether an annotation is
a geometry field and which kinds it holds (`get_geo_info(PolygonField)` is `"polygon"`).

### Geometry values and GeoJSON

```python
from surreal_orm_lite import Geometry, GeometryLine, GeometryPolygon

times_square = GeometryPoint(-73.9855, 40.7580)  # longitude, latitude: the GeoJSON order
midtown = GeometryPolygon.parse_coordinates(
    [[(-74.0, 40.745), (-73.97, 40.745), (-73.97, 40.765), (-74.0, 40.765), (-74.0, 40.745)]]
)
broadway = GeometryLine(GeometryPoint(-73.9897, 40.74), GeometryPoint(-73.9845, 40.76))

times_square.to_geojson()  # {'type': 'Point', 'coordinates': [-73.9855, 40.758]}
Geometry.from_geojson({"type": "Point", "coordinates": [-73.9855, 40.758]}) == times_square  # True
str(times_square)          # "{type: 'Point', coordinates: [-73.9855, 40.758]}": its SurrealQL text

await Restaurant(id="pizza", name="Corner Pizza", location=GeometryPoint(-73.9877, 40.7547)).save()
await Restaurant(id="burgers", name="Park Burgers", location=(-73.9881, 40.7417)).save()  # a pair
await Restaurant(
    id="deli", name="Village Deli", location={"type": "Point", "coordinates": [-73.9969, 40.729]}
).save()  # a GeoJSON object
await Restaurant(id="bistro", name="Paris Bistro", location=GeometryPoint(2.3522, 48.8566)).save()
await Restaurant(id="truck", name="Food Truck").save()  # no location
await Zone(id="midtown", name="Midtown", area=midtown).save()
await Route(id="broadway", name="Broadway", path=broadway, stops=[times_square]).save()

deli = await Restaurant.objects().get("deli")
deli.location                             # GeometryPoint(longitude=-73.9969, latitude=40.729)
deli.model_dump()["location"]             # the same GeometryPoint
deli.model_dump(mode="json")["location"]  # {'type': 'Point', 'coordinates': [-73.9969, 40.729]}
```

The seven classes, `GeometryPoint`, `GeometryLine`, `GeometryPolygon`, `GeometryMultiPoint`,
`GeometryMultiLine`, `GeometryMultiPolygon` and `GeometryCollection`, subclass `Geometry` and have
the names, constructor signatures, attributes and methods of the SDK's classes (`surrealdb.data`):
`GeometryPoint(longitude, latitude)`, `parse_coordinates()`, `get_coordinates()`,
`geometry_points`, … They are the ORM's own classes because Pydantic's `model_dump()` turns the
SDK's dataclasses into plain dicts; the ORM converts them to the SDK's classes when it sends them,
and SurrealDB stores native geometries. They are immutable (setting an attribute raises
`AttributeError`, and an attribute holding several items is a tuple) and hashable; `copy`, `pickle`
and Pydantic's copies keep them, so a geometry works as a field default; `repr()` is the SDK's
format.

Coordinates are real numbers, stored as `float`. A `bool`, a string, `None` or a `Decimal` raises
`TypeError` (convert a `Decimal` with `float()`). A non-finite coordinate, a polygon ring of fewer
than 4 points or whose last point is not its first, and a `GeometryCollection` nested more than 32
levels deep raise `InvalidGeometryError`, which is both a `ValueError` and a
`surrealdb.InvalidGeometryError`. GeoJSON goes both ways: `to_geojson()` and `__geo_interface__`
(read by shapely and similar libraries) give the GeoJSON dict, and `Geometry.from_geojson()` reads
one (`GeometryPolygon.from_geojson()` requires a polygon); a 3D position, an unknown `type` or a
`Feature` raises `InvalidGeometryError`.

A geometry field accepts the ORM value, the SDK's, a GeoJSON object and, for points, a
`(longitude, latitude)` pair; anything else, or a refused value, raises Pydantic's
`ValidationError`. `model_dump()` returns the ORM value and `model_dump(mode="json")` its GeoJSON
object, which `model_validate_json()` reads back.

**Equality with the SDK's values is one-way**, as for `Duration`:
`GeometryPoint(1, 2) == surrealdb.data.GeometryPoint(1.0, 2.0)` is `True`, the reverse is `False`.
Keep the ORM value on the left.

### `nearby()`

```python
near = await Restaurant.objects().nearby("location", times_square, max_distance=2_000).exec()
sorted(r.name for r in near)  # ['Corner Pizza', 'Park Burgers']: within 2 km of Times Square
```

`nearby(field, point, max_distance)` keeps the records whose field is a point within
`max_distance` metres of `point`, as SurrealDB's `geo::distance` measures it. The point is a
`GeometryPoint` (the ORM's or the SDK's), a `(longitude, latitude)` pair or a GeoJSON `Point`; it
and the distance are bound as query variables, never written into the statement. A `.is_point()`
guard leaves out the records whose field is not a point (missing, `NULL`, a polygon, a string, a
GeoJSON object), on both lines. Several calls are AND-ed, and the condition joins `filter()`
wherever that applies: `exec()`, `first()`, `get()` without an id, `count()` and the other
aggregations, `values()`/`annotate()` groups, `bulk_update()`, `bulk_delete()`, `patch()`, and
`live()`/`watch()`. `all()` ignores it, as it ignores `filter()`, and `changes()` refuses it. A
field the model declares as a geometry that holds no point (`PolygonField`, `GeoField["line"]`)
raises `ValueError`, and so does a negative or non-finite distance.

### Distance: GeoDistance

```python
from surreal_orm_lite import GeoDistance

closest = await (
    Restaurant.objects()
    .nearby("location", times_square, max_distance=5_000)
    .annotate(dist=GeoDistance("location", times_square))
    .order_by("dist")
    .limit(3)
    .exec()
)
[(r.name, round(r.dist)) for r in closest]  # [('Corner Pizza', 411), ('Park Burgers', 1826), ('Village Deli', 3365)]

farthest_first = await Restaurant.objects().annotate(dist=GeoDistance("location", times_square)).order_by("-dist").exec()
farthest_first[0].name                             # 'Paris Bistro'
farthest_first[-1].name, farthest_first[-1].dist  # ('Food Truck', None): no location, last
```

`annotate(<alias>=GeoDistance(field, point))` gives each instance the distance as an attribute
named after the alias (`r.dist`): metres as a `float`, or `None` when the record's field is not a
point. `order_by("dist")` sorts the nearest first, after the records without a point;
`order_by("-dist")` sorts the farthest first and the records without a point last. The attribute is
never written back: `model_dump()`, `update()` and `upsert()` ignore it. Rows that do not validate
(a `select()` that omits a required field) come back as dicts carrying the alias.

The alias is a projection, not a column: `filter(dist__lt=500)` filters a stored column named
`dist`, so bound the distance with `nearby()`. Choose an alias no record stores: on an
`extra="allow"` model, a stored, undeclared column of that name is replaced by the distance in the
results and missing from the instance, so a later `update()` or `upsert()` removes it. An alias
that names a declared field or its column, `id`, `in` or `out`, starts with `_` or `model_`, or
names a model attribute (`save`, `objects`, …) raises `ValueError`. A `GeoDistance` cannot be
combined with aggregations (`Count()`, …) or `values()`, and `live()`, `watch()` and `changes()`
refuse `annotate()`.

### Spatial lookups

```python
inside = await Restaurant.objects().filter(location__inside=midtown).exec()    # Corner Pizza
outside = await Restaurant.objects().filter(location__outside=midtown).exec()  # the four others
zones = await Zone.objects().filter(area__contains=times_square).exec()         # Midtown
crossing = await Route.objects().filter(path__intersects=midtown).exec()        # Broadway
```

`inside`, `outside` and `intersects` compare a field with a geometry through SurrealQL's `INSIDE`,
`OUTSIDE` and `INTERSECTS`; the existing `contains` lookup tells whether a polygon field contains
a point (`area__contains=times_square`). The value of a spatial lookup is an ORM or SDK geometry,
a GeoJSON object or a `Var`, bound as one value; the lookups work in `filter()`, in `Q` objects
(`~Q(...)` included) and in live queries. A list, a tuple, a string or `None` raises `ValueError`
(use `__in` for membership in a list), and so does a spatial lookup on the record id or on an
edge's `in`/`out`.

`outside` keeps SurrealQL's meaning: it is true for every record whose field is not inside the
geometry, including a record whose field is missing, `NULL`, a string or a GeoJSON object stored as
a plain object. That is why Food Truck, which has no location, is among `outside`;
`~Q(location__inside=midtown)` returns the same records.

### Geometries in live queries

```python
import asyncio

async with Restaurant.objects().nearby("location", times_square, 2_000).live() as stream:
    await Restaurant(id="crepes", name="Seine Creperie", location=GeometryPoint(2.35, 48.86)).save()
    await Restaurant(id="cart", name="Hot Dog Cart", location=GeometryPoint(-73.9851, 40.7589)).save()
    async with asyncio.timeout(5):
        async for event in stream:
            print(event.instance.name)  # Hot Dog Cart: the Paris write is not notified
            break
```

`nearby()`, the spatial lookups and geometry filter values work in `live()` and `watch()`
([section 21](realtime.md#21-live-queries-real-time-notifications)), where the ORM writes each value into the
statement as a GeoJSON object literal (`{type: 'Point', coordinates: [-73.9855, 40.758]}`), with
the same notifications on both lines. Typed live streams and change feeds
([section 22](realtime.md#22-change-feeds-changes)) deliver instances holding the ORM's classes.

### Filter values, keyword writes and assignments

```python
same_spot = await Restaurant.objects().filter(location=GeometryPoint(-73.9877, 40.7547)).exec()  # Corner Pizza

pizza = await Restaurant.objects().get("pizza")
await pizza.merge(location=GeometryPoint(-73.9876, 40.7548))  # a geometry object, not a pair
pizza.entrance = GeometryPoint(-73.9877, 40.7546)             # the same for an assignment
await pizza.update()
```

Give geometry objects wherever the field does not validate the value. The values of the other
lookups (`exact`, `contains`, …) are bound as given, and keyword writes (`merge()`, `bulk_update()`, the `atomic_*`
helpers) are not validated against the field
([#220](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/220),
[#221](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/221)): a
`(longitude, latitude)` pair or a GeoJSON dict there is sent as an array or an object, which
matches nothing or is stored as such. Models do not validate assignments, so
`pizza.entrance = (-73.9877, 40.7546)` keeps the tuple, which Pydantic warns about when it
serialises it and which is stored as an array. A geometry filter value matches a stored geometry
exactly.

### Decimal coordinates of SDK geometries

The ORM's classes refuse a `Decimal` coordinate, and so does a geometry field given an SDK
geometry that holds one. An SDK geometry (`surrealdb.data.GeometryPoint`, …) given as a filter
value, a `raw_query()` variable or a keyword write is only checked for what SurrealDB refuses
(below), and SurrealDB converts its `Decimal` coordinates to floats. A `Decimal` coordinate written with an exponent goes out in
fixed-point notation, as a top-level `Decimal` does (see [Precision and `Decimal`](#precision-and-decimal)):
before v0.24.0, SurrealDB 2.x refused `Decimal("1E-7")` there, and both lines stored a long one
(`Decimal("1.234567890123456789012345678901E-7")`) with a wrong value; both lines now store the
value. An SDK geometry SurrealDB would refuse raises `ValueError` before anything is sent: a line
of fewer than 2 points, a polygon without a ring, a ring of fewer than 4 points once closed, a
coordinate that is a `bool`, a string, `None` or an integer beyond 64 bits, or a member that is not
a geometry of the expected kind.

### Computed geometry fields

```python
await Zone(id="unmapped", name="Unmapped").save()

(await Zone.objects().get("midtown")).centroid   # a GeometryPoint at (-73.985, 40.755), computed by SurrealDB
(await Zone.objects().get("unmapped")).centroid  # None: the guard skips a zone without an area
```

`Zone.centroid` is a computed field ([section 16](models.md#16-computed-fields-computed--define-field--value))
read back as an ORM `GeometryPoint`. Guard geometry functions with a type check: `geo::centroid()`
raises on a record without a polygon, so `computed("geo::centroid(area)")` would make every write
of a zone without an area fail, while `IF area.is_polygon() THEN … END` leaves its centroid
`None`.

### Migrating stored GeoJSON

```python
for restaurant in await Restaurant.objects().all():
    await restaurant.update()  # a location read from a GeoJSON object is written back as a native point
```

SurrealDB keeps a GeoJSON dict as a plain object, not a geometry: the full SurrealDB-ORM stores its
geometry fields that way, and so does any client that writes GeoJSON dicts. `nearby()` skips such
objects, `geo::*` functions and `geometry<…>` columns refuse them, and no SurrealQL cast turns them
into geometries. A geometry field reads them (a GeoJSON object validates, in a read and in the row
a write returns), and `update()` writes the instance back with a native geometry, so loading and
updating every record migrates a table.

### Untyped fields

A field declared `Any` or `dict`, or an undeclared column of an `extra="allow"` model, is not a
geometry field. A GeoJSON dict written there stays a plain object (see
[Migrating stored GeoJSON](#migrating-stored-geojson)); an ORM geometry written there is stored
as a native geometry but reads back as the SDK's class (`surrealdb.data.GeometryPoint`, …), which
a later write of the instance stores as a plain object
([#222](https://github.com/EulogySnowfall/SurrealDB-ORM-lite/issues/222)). Declare geometry fields
with the annotations above. The results of `raw_query()`, `query()` and `call_function()` are
untyped too: they hold the SDK's classes, which compare equal to the ORM's with the ORM value on
the left.

### Type checkers

mypy sees each alias as its class or `None` (`PointField` is `GeometryPoint | None`). Without the
Pydantic mypy plugin, mypy only knows a default written in the class body: write
`location: PointField = None` (or `= Field(default=None, alias="loc")`) in code that builds the
model without that field; at runtime the default is there either way. Type checkers do not
understand `GeoField["point"]` (mypy: `"GeoField" expects no type arguments`), as in the full ORM:
in type-checked code, use the aliases or the classes.

### Geometries the SDK cannot read

A degenerate geometry written by another client (a line of fewer than 2 points, alone or inside
another geometry, or a polygon ring of fewer than 4 points) cannot be decoded by the surrealdb SDK.
The clients the ORM opens read it as in 0.40.x, as its GeoJSON dict, such as
`{"type": "LineString", "coordinates": [[1.0, 2.0]]}`; a multi-geometry or a collection holding
one reads as GeoJSON as a whole (see
[Values the SDK cannot read](#values-the-sdk-cannot-read) for where this applies):

- an untyped field (`Any`, `dict`), `raw_query()` and live notifications give that dict;
- a geometry field refuses it with a `ValidationError` naming the field, since the ORM's geometry
  classes refuse degenerate values: `get()` raises it, and `filter().exec()` and `raw_query()`
  return such rows as dicts, as for any row that does not validate.

The ORM never writes one: its classes refuse such values, and so does the check of the SDK
geometries it sends (see [Decimal coordinates of SDK geometries](#decimal-coordinates-of-sdk-geometries)).
Written back, the dict an untyped field holds is stored as a plain object, not a geometry, on both
lines, like any GeoJSON dict (see [Migrating stored GeoJSON](#migrating-stored-geojson)):
`merge(v=row.v)` does it, and so does `update()` after a change to another field, since it
rewrites the whole row.

### Coming from the full SurrealDB-ORM

- **Field values are geometry objects, not GeoJSON dicts.** `location.to_geojson()` gives the
  dict, `model_dump(mode="json")` writes GeoJSON, and a GeoJSON dict (or a `(longitude, latitude)`
  pair for points) is accepted as input, so full-ORM-shaped data keeps loading. SurrealDB keeps a
  GeoJSON dict as a plain object, which `geo::*` functions and `geometry<…>` columns refuse; lite
  stores a native geometry (see [Migrating stored GeoJSON](#migrating-stored-geojson)).
- **`get_geo_info()` returns SurrealDB's kind names**: `"line"` for `LineStringField` (the full
  ORM: `"linestring"`), `"point|polygon"` for a union; `GeoField["linestring"]` is still accepted.
- **`nearby()`** keeps its name and argument order. The point and the distance are bound, never
  written into the statement; a `.is_point()` guard skips the records whose field is not a point;
  several calls are AND-ed (the full ORM keeps the last); a field declared as a geometry that holds
  no point raises `ValueError`; the point may also be an ORM or SDK `GeometryPoint` or a GeoJSON
  point.
- **`GeoDistance`**: its value reaches each instance as an attribute named after the alias (the
  full ORM computes it but does not attach it), `None` when the record's field is not a point;
  `.point` is an ORM `GeometryPoint` (the full ORM keeps the tuple); the aliases that would hide a
  column, the id, an edge endpoint or a model attribute are refused; it cannot be combined with
  aggregations or `values()`.
- **Lite additions**: the eight value classes, `MultiLineStringField`, `MultiPolygonField`,
  `GeometryCollectionField`, `GeometryField`, `InvalidGeometryError` and the `inside`, `outside`
  and `intersects` lookups.
- **Aliases inside containers** (`list[PointField]`) make Pydantic warn at class definition; use
  the classes there (`list[GeometryPoint]`).
- **Imports**: `from surreal_orm_lite import GeoField, PointField, GeoDistance, …` (the full ORM:
  `surreal_orm`, `surreal_orm.fields`, `surreal_orm.geo`); `is_geo_field` and `get_geo_info` come
  from `surreal_orm_lite.geo` (the full ORM: `surreal_orm.fields`). There is no
  `surreal_orm_lite.fields` module.
