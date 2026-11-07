# Live queries and change feeds

Real-time notifications with live queries, and change feeds (`changes()`).

## 21. Live queries (real-time notifications)

A live query is a server-side subscription: SurrealDB pushes a notification every time a
record it watches is created, updated or deleted. It needs a **WebSocket** connection, and it
works on both SurrealDB 2.x (2.6 / 2.7) and 3.x (3.2 / 3.3).

### Typed events: `live()`

Since v0.20.0, `live()` streams **model instances**, with the same API as the full
SurrealDB-ORM. The queryset's filters are applied by the server, and the live query is killed
when the block exits — even if the body raised:

```python
from surreal_orm_lite import LiveAction

async with User.objects().filter(role="admin").live() as stream:
    async for event in stream:
        match event.action:
            case LiveAction.CREATE:
                print("new admin:", event.instance.name)
            case LiveAction.UPDATE:
                print("admin updated:", event.instance)
            case LiveAction.DELETE:
                print("admin removed:", event.record_id)
```

Each item is a `ModelChangeEvent`:

| Attribute          | Type                      | Meaning                                                                                         |
| ------------------ | ------------------------- | ----------------------------------------------------------------------------------------------- |
| `action`           | `LiveAction`              | `CREATE`, `UPDATE` or `DELETE`                                                                  |
| `instance`         | your model                | the record after the change; for `DELETE`, its last state                                       |
| `record_id`        | `str`                     | the affected record, e.g. `"user:abc"`                                                          |
| `changed_fields`   | `list[str]`               | diff mode only: the top-level fields that changed, as Python field names                        |
| `raw`              | `dict / list`             | the payload as received: a dict, or the patch list in diff mode (a 2.x diff `DELETE` is a dict) |
| `validation_error` | `ValidationError \| None` | set when the record does not fit the model; `None` otherwise                                    |

A record that does not validate against the model does **not** end the stream — one bad row
written by another client, or a `fetch()`-resolved link on a field typed `str`, would otherwise
stop your `async for` and kill the subscription. The event carries the error in
`validation_error` and an instance built **without validation** from the record as received,
the way `exec()` falls back to plain rows. Check `event.validation_error` before trusting
`event.instance`; `post_live_change` handlers are not called for such events, so they only ever
see validated instances. A warning naming the failing fields (never their values) is logged once
per stream.

**What the server filters.** `filter()` (keyword lookups, `Q` objects, `Var` references with
`variables()`) and `fetch()` are honoured, with field aliases translated as everywhere else.
The filter is evaluated against each change, which has two consequences worth knowing:

- a record that **enters** the filter through an update is reported as `UPDATE`, not `CREATE`;
- a record that **leaves** it reports nothing — not even a `DELETE` — and a later `DELETE` of
  that record is not reported either, because it no longer matched.

**Filter values are written into the statement.** SurrealDB 2.x does not carry bound parameters
into a live query: `LIVE SELECT … WHERE status = $s` is accepted, then `$s` evaluates to `NONE`
at notification time and the filter matches nothing, silently. So for live queries only, the ORM
writes each value as a SurrealQL literal — the same approach as the full SurrealDB-ORM — and the
filter behaves identically on both lines. The encoder is closed: `None`, `bool`, `int`, `float`,
`str`, `Decimal`, `datetime` (written in UTC), `UUID`, `RecordID`, and lists or dicts of these;
since v0.23.0 also `timedelta` and `Duration` (`1h30m`), `date` (`d'2026-02-01T00:00:00Z'`), sets
(sorted arrays) and `Range` (`9..17`, `65..`), see [section 24](fields.md#24-rich-field-types). Anything
else, a string containing a NUL character, an integer beyond 64 bits, a `Decimal` SurrealDB would
round or a zero, negative or oversized duration raises `TypeError` when you call `live()`, before
anything is sent. A `Var` must be supplied through `variables()` — an unbound reference would read
as `NONE` on every notification, so `age > $min_age` would match every record; it raises
`SurrealDbError` instead. SurrealDB's own parameters need no `variables()`: `Var("auth")` (and
`$access`, `$session`, `$token`) stays a reference the server reads on every notification, so
`filter(owner=Var("auth"))` streams the signed-in record user's own records, on both lines; the
string `"$auth"` is data, written as `'$auth'` (see
[String filter values and `Var()`](querying.md#string-filter-values-and-var)). Every other query keeps binding
parameters.

### Diff mode

`live(diff=True)` asks the server for patches instead of whole records (`LIVE SELECT DIFF`):

```python
async with Product.objects().live(diff=True) as stream:
    async for event in stream:
        if event.action == LiveAction.UPDATE:
            print(event.record_id, "changed", event.changed_fields)  # ["price", "stock"]
```

A `CREATE` still carries the whole record, so `event.instance` is complete. An `UPDATE` carries
only patches: `event.instance` holds just the `id`, `changed_fields` lists what changed, and
`event.raw` is the patch list. The format is JSON Patch **extended by SurrealDB**: a changed
string arrives as `{"op": "change", "value": "<diff-match-patch text>"}` rather than a
`replace`. A `DELETE` gives an instance holding the `id` on 3.x, and its last state on 2.x (see
the behaviour table).

### The `post_live_change` signal

Handlers connected to `post_live_change` run for every event a `live()` stream yields — changes
made by any client, unlike `post_save` and friends, which fire for this process's own writes:

```python
from surreal_orm_lite import LiveAction, post_live_change

@post_live_change.connect(Player)
async def on_player_change(sender, instance, action, record_id, changed_fields, **kwargs):
    if action == LiveAction.CREATE:
        await broadcast({"type": "player_joined", "name": instance.name})
```

Handlers run on one background task per stream, **in event order** — a slow handler for a
`CREATE` never lets the `DELETE` that follows overtake it — and never stall the iteration. One
that raises is logged and does not interrupt anything. Leaving the block normally waits for the
pending handlers (up to `stream.signal_drain_timeout`, 5 s by default) so the last event's side
effects are not lost; leaving it on an exception cancels them. The signal fires only while you
iterate a `live()` stream.

### Surviving a dropped connection

Since v0.21.0 a `live()` or `watch()` stream survives a WebSocket that drops — a network
failure, a server restart, a proxy timing out. The ORM notices within milliseconds, opens a new
connection (signed in, session restored), sends the same live query again and carries on: an
`async for` parked on the stream simply receives the next event later. `stream.live_id` changes.

```python
async def catch_up(old_id, new_id):
    # A live query only reports what happens while it runs: re-read what the outage hid.
    await refresh_dashboard(await Order.objects().filter(status="open").all())

async with Order.objects().filter(status="open").live(on_reconnect=catch_up) as stream:
    async for event in stream:
        ...
```

- **`on_reconnect(old_id, new_id)`** — sync or async, called with both `UUID`s once the new
  subscription is already buffering, so nothing written after it is missed while it runs. An
  exception in it is logged, never raised.
- **The outage itself is not replayed.** Changes made while the connection was down are lost to
  a live query. Catch up in `on_reconnect`, or pair the stream with a change feed (below), which
  can be resumed exactly from a cursor.
- **Backoff:** `reconnect_delay` (0.5 s) doubling up to `reconnect_max_delay` (30 s), at most
  `reconnect_max_attempts` (10; `None` retries forever) — class attributes on `LiveModelStream`
  and `LiveStream`, overridable per stream. Only connection failures are retried. When the
  attempts run out, or anything else goes wrong (the table was removed, the session token was
  refused), the error is raised from the iteration and `is_active` turns `False`.
- **Never under another identity.** A live query runs with the permissions of whoever opened it,
  and a new connection comes back as the identity the manager replays (the stored session
  token, else the configured user). If the stream was opened under an identity a new connection
  cannot restore (`signin(..., store=False)`), or what a new connection would restore is no
  longer that identity (another stored token, other connection settings, a refused session
  replay), the stream ends with `SurrealDbAuthenticationError` instead of resubscribing as
  somebody else. The comparison is on the settings themselves, so changing them and changing them
  back — or `signin()` then `invalidate()` — counts as no change. It is per connection, so another
  event loop's identity changes do not leak in. Identity changes made directly on the SDK client
  are not seen by the ORM.
- **`auto_resubscribe=False`** ends the stream on a drop with `SurrealDbConnectionError`, as does
  any secondary reader from `subscribe_live(uuid)`, which is tied to one uuid. Before v0.21.0
  they all waited forever.
- **Teardowns you ask for still end the stream.** `kill()`, leaving the block,
  `close_connection()`, `reconnect()`, `set_url(reconnect=True)` — none of them triggers a
  resubscribe. `stop()` during a reconnect cancels it.
- A silent network loss with no TCP reset is noticed by the WebSocket keepalive, within about
  40 s.

The connection manager benefits too: `get_client()` now replaces a dropped WebSocket client
instead of handing back a dead one, and concurrent callers share one new connection. A request
that was already in flight when the socket dropped still fails — with a retryable `SurrealDbConnectionError` since
v0.50.0 (SDK 2.0 raised its own `KeyError`) — because retrying arbitrary queries is planned for v0.51.0 (connection
resilience).

**One exception: an identity the manager does not store.** `signin()`, `signup()` and
`authenticate()` with `store=False` change the connection's own identity (that is the SDK's
behaviour) without telling the manager to restore it. If that connection drops, a new one could
only come back as the configured user — a silent change of privileges. So every call on that
event loop raises `SurrealDbAuthenticationError` until the application chooses: sign in or
authenticate again, `invalidate()`, or `reconnect()`/`close_connection()` to continue as the
configured user. Live streams on that connection end the same way. To mint tokens for other
users without touching the shared connection, use the model-level auth (section 19), which runs
on its own short-lived connections.

### Migrating from SurrealDB-ORM

`live()`, `ModelChangeEvent`, `LiveModelStream`, `LiveAction` and `post_live_change` have the
same names, fields and arguments as in the full ORM — change the import from `surreal_orm` to
`surreal_orm_lite`. The differences:

- `auto_resubscribe=` and `on_reconnect=` are accepted with the same defaults (v0.21.0).
  `on_reconnect` receives `UUID`s rather than `str`, like lite's `live_id`; `str(old_id)` works
  with both.
- Lite **refuses** a clause a live query cannot honour (`order_by()`, `limit()`, …) where the
  full ORM ignores it, and honours `fetch()`.
- `record_id` is filled in diff mode too.

### Raw envelopes: `watch()`

`watch()` takes the same filters and `diff=` but yields the server's **raw envelope**, a plain
dict, instead of an event:

```python
async with User.objects().filter(role="admin").watch() as stream:
    async for notif in stream:
        print(notif["action"], notif["result"])
        if notif["action"] == LiveAction.DELETE:
            break
# the live query is killed here, even if the body raised
```

| Key       | Type       | Meaning                                                                               |
| --------- | ---------- | ------------------------------------------------------------------------------------- |
| `action`  | `str`      | `CREATE`, `UPDATE` or `DELETE` — compare against `LiveAction`                         |
| `id`      | `UUID`     | the live query's own uuid                                                             |
| `record`  | `RecordID` | the affected record                                                                   |
| `result`  | `dict`     | the record after the change; for `DELETE`, its last content; patches with `diff=True` |
| `session` | `UUID`     | SurrealDB 3.x only — absent on 2.x                                                    |

Values keep their SDK types (`RecordID`, `Datetime`) rather than being coerced. With
`diff=True` the raw payload differs by line — see the behaviour table; `live(diff=True)`
normalises it.

`LiveAction` is a `StrEnum`, so it compares directly against the raw string and you never need
to quote a magic value.

### The explicit form

When the subscription must outlive one block — read in one task, stopped from another — start
the stream yourself, hand **the stream** to the reader, and kill it by uuid from anywhere:

```python
from surreal_orm_lite import SurrealDBConnectionManager

stream = await User.objects().filter(role="admin").watch().start()  # or .live().start()

async def reader() -> None:
    async for notif in stream:  # ends when the live query is killed
        handle(notif)

task = asyncio.create_task(reader())
...
await SurrealDBConnectionManager.kill(stream.live_id)
```

A started stream buffers every notification until it is read, so always read **it** rather than
opening a second reader on its uuid and leaving the stream's own buffer to grow.
`SurrealDBConnectionManager.subscribe_live(live_id)` adds another reader to the same live query
— useful to fan one subscription out to several consumers, each of which must keep reading.

`await User.objects().live()`, the v0.19.0 way to get the uuid, still works but is deprecated
(`DeprecationWarning`). The returned stream also behaves as a coroutine for that path, so
`asyncio.create_task(qs.live())` and `asyncio.run(qs.live())` keep working too. Like a coroutine
it can be awaited once, and an awaited stream refuses to `start()` (and the reverse): the two
would be separate subscriptions, and leaving the block would kill only one of them.

`subscribe_live()` is **not** a coroutine: call it without `await`. Buffering starts the moment
you call it rather than at the first iteration, so a write you make in between is still
reported. Each reader on a uuid receives every notification.

A read that times out loses nothing, so polling with a deadline is safe:

```python
stream = SurrealDBConnectionManager.subscribe_live(live_id)
while True:
    try:
        notif = await asyncio.wait_for(anext(stream), timeout=1.0)
    except TimeoutError:
        continue  # a quiet second; the stream is still alive
    handle(notif)
```

`await stream.aclose()` stops one reader and drops its buffer without killing the live query.

`kill()` is idempotent — an unknown or already-killed uuid is a no-op — and it ends every open
stream on that uuid, so the `async for` above terminates rather than waiting forever.

### What a live query will refuse

- **A non-WebSocket connection.** Live queries cannot run over HTTP; `live()`, `watch()` and
  `subscribe_live()` say so instead of letting the SDK raise a bare `NotImplementedError`.
- **A clause it cannot honour.** SurrealDB's `LIVE SELECT` has no `ORDER BY`, `LIMIT` or
  `START`, and a typed stream needs whole records, so `select()`, `limit()`, `offset()`,
  `order_by()`, `values()` and `annotate()` are rejected by name rather than silently ignored.
- **A transaction.** A live query is a connection-level subscription with no transaction to
  join, so `objects(tx=tx).live()` is refused rather than quietly run outside it.
- **A filter value with no SurrealQL literal form** (see above) — `TypeError` at the call.
- **A table that does not exist, on SurrealDB 3.x.** You get a `SurrealDbNotFoundError` naming
  the table. On 2.x the same call succeeds and simply never notifies. Define the table first
  if you need the two lines to behave alike.

### The stream handles

`live()` returns a `LiveModelStream` and `watch()` a `LiveStream`, both exported from the package
root. They share these members:

| Member      | Type             | Meaning                                                       |
| ----------- | ---------------- | ------------------------------------------------------------- |
| `live_id`   | `UUID \| None`   | the live query's uuid; `None` before start and after `stop()` |
| `table`     | `str`            | the table being watched                                       |
| `is_active` | `bool`           | whether the subscription is running                           |
| `start()`   | `async` → self   | open the subscription; a no-op on a running stream            |
| `stop()`    | `async` → `None` | kill the subscription; safe before start and to repeat        |

`__aexit__` calls `stop()`, so the `async with` form needs none of these. They are there for the
cases where you hold the stream yourself.

### Lifecycle and limitations

A reader parked on `async for` only wakes when something wakes it, so it is worth knowing
exactly what ends a stream.

- **`kill()` ends it**, and so does leaving a `live()` or `watch()` block. This works identically on both
  server lines.
- **Closing the connection ends it.** `close_connection()` releases every reader on its event
  loop before the socket goes, and `close_all_connections()` releases every reader on every
  loop, so an ordinary shutdown does not leave a wedged task behind. Leaving a `watch()` block
  afterwards does not reconnect: with the connection gone there is nothing left to kill.
- **A timed-out read does _not_ end it.** Cancelling a pending read loses nothing; the next read
  picks up where it left off.
- **A WebSocket that drops on its own does _not_ end it** (v0.21.0): the stream resubscribes —
  see [Surviving a dropped connection](#surviving-a-dropped-connection). With
  `auto_resubscribe=False`, and for `subscribe_live()` readers, it raises
  `SurrealDbConnectionError` instead.
- **Subscriptions are per event loop.** A uuid belongs to the connection that created it, on the
  loop that created it. Killing it from another loop is not supported.
- **A failed `kill()` releases the readers but keeps the uuid alive.** When the server refuses
  the kill for any reason other than an unknown uuid, the error is raised, the readers are woken,
  and the subscription is still running — so you can subscribe to it again or retry `stop()`.
  A kill caught by a dropped connection is not a refusal: the subscription died with the socket,
  so `kill()` records it as ended and returns normally.
- **The buffer is unbounded.** A stream you stop reading but never kill keeps accumulating
  notifications. Use the `async with` forms, or pair every uuid you start with a `kill()`.

## 22. Change feeds (`changes()`)

A change feed is SurrealDB's durable log of a table's writes, kept for a retention period you
choose. Unlike a live query, nothing is missed while nobody reads: a consumer that stops and
restarts resumes exactly where it left off. It suits event streaming, replication and audit.

```sql
DEFINE TABLE order SCHEMALESS CHANGEFEED 7d;   -- only writes made after this are recorded
```

```python
stream = Order.objects().changes(since=load_cursor())   # None = from now
async for event in stream:
    await publish(event.action, event.instance)          # ModelChangeEvent, as for live()
    save_cursor(stream.cursor)                           # after processing
```

- **`stream.cursor`** is an `int`: pass it back as `since=` to resume. It moves past a
  transaction only once that transaction's last event has been yielded, so saving it after each
  event gives **at-least-once** delivery: nothing is ever skipped, and a transaction cut short by
  a crash is delivered again in full.
- **`since=`** takes a cursor, a `datetime` or ISO-8601 string (naive means UTC), or `None` for
  changes made after the stream starts. A datetime is exact on SurrealDB 3.x and at-least-once on
  2.x, whose server also returns some earlier changes.
- **What an event says.** A change feed records writes, not their kind: a creation arrives as
  `UPDATE`, a deletion as `DELETE`. With `CHANGEFEED … INCLUDE ORIGINAL` on the table, updates
  also carry `changed_fields`, and on SurrealDB 3.3 a deletion carries the last state of the
  record (elsewhere the instance holds only its `id`).
- **`poll_interval`** (0.1 s) is the pause when the feed has nothing new; **`batch_size`** (100)
  the entries read per poll. `stop()` ends the iteration at once when it is waiting between
  polls or in a retry backoff, or as soon as a request already sent returns; `start()` fixes the
  starting position before the first read.
- **A read under `asyncio.timeout()` or `asyncio.wait_for()`** times out as asked and loses
  nothing: the stream keeps going, and the next read starts from the cursor. Before v0.22.2 a
  read cancelled just as the server answered could be retried as a dropped connection instead,
  and never time out.
- Works over **HTTP** as well as WebSocket. A dropped connection is retried with the same
  backoff as live streams, and nothing is lost, because the next read starts from the cursor.
- **Refused:** any queryset clause — `SHOW CHANGES` has no `WHERE`, so `filter()` would silently
  stream the whole table — and a table that is missing or has no change feed (on the table or
  on its database), with the `DEFINE TABLE` to run.
- **Writing to a change-feed table on SurrealDB 2.x: wrap the write in `retry_on_conflict`.**
  On 2.x, concurrent writes to a `CHANGEFEED` table conflict with each other, even on different
  records. Measured on 2.7.0 with 8 writers sharing one connection: 2.5 to 4 % of `save()` and
  `merge()` calls were refused with a retryable conflict, in every second of the run. A single
  writer was refused far more rarely: at most once per cycle of the server's change-feed garbage
  collection (`--changefeed-gc-interval`, 10 s by default), and not on every cycle. The same
  8-writer load refused nothing on a table without a change feed, nor on 3.3.4 (`save()`,
  `merge()` and `delete()`). The ORM does not retry these writes for you: apply
  `retry_on_conflict` yourself (see [section 14](writing.md#14-optimistic-concurrency-retry_on_conflict)).
  A refused write stores nothing, so replaying it is safe for the data; the `pre_save` and
  `around_save` handlers run again on each attempt. Outside a transaction the error is the SDK's
  own `InternalError` ("… This transaction can be retried"), which `is_conflict_error()`
  recognises; once the retries run out, `retry_on_conflict` raises `SurrealDbConflictError`.

  ```python
  from surreal_orm_lite import retry_on_conflict

  @retry_on_conflict()
  async def save_order(order: Order) -> Order:
      return await order.save()

  await save_order(Order(id="o1", total=42))
  ```

**Why the cursor is not the server's `versionstamp`.** SurrealDB 2.x and 3.x number their feeds
differently: resuming a 2.x feed with the stamp it returned yields nothing at all, and asking
3.x for changes since a datetime always answers empty. `ChangeModelStream` normalises both (see
the behaviour table), so the same code resumes correctly on either line.
