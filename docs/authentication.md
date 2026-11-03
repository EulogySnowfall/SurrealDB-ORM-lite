# Authentication

Connection-level authentication and the model-level `AuthenticatedUserMixin`.

## 18. Authentication (`signin` / `signup` / `info` / `invalidate`)

Authenticate the connection as a SurrealDB **record user** (a `DEFINE ACCESS … TYPE RECORD`
method) or as a different **system user**. Declare the access method once — DDL goes through
`query()`:

```python
client = await SurrealDBConnectionManager.get_client()
await client.query("""
    DEFINE TABLE OVERWRITE app_user SCHEMALESS
      PERMISSIONS FOR select, update WHERE id = $auth.id;
""", {})
await client.query("""
    DEFINE ACCESS OVERWRITE account ON DATABASE TYPE RECORD
      SIGNUP ( CREATE app_user SET email = $email, pass = crypto::argon2::generate($pass) )
      SIGNIN ( SELECT * FROM app_user
               WHERE email = $email AND crypto::argon2::compare(pass, $pass) )
      DURATION FOR TOKEN 15m, FOR SESSION 12h;
""", {})
```

Then register and authenticate users:

```python
from surreal_orm_lite import AuthTokens, SurrealDBConnectionManager, SurrealDbAuthenticationError

tokens = await SurrealDBConnectionManager.signup(
    access="account", variables={"email": "ada@example.com", "pass": "s3cret"},
)
tokens = await SurrealDBConnectionManager.signin(
    access="account", variables={"email": "ada@example.com", "pass": "s3cret"},
)
tokens.access     # the JWT — hand it to a web client
tokens.refresh    # SurrealDB 3.x only (WITH REFRESH); always None on 2.6.x
```

`namespace`/`database` come from `set_connection()` for record access. A **system user** gets
neither unless you pass them, because a root user is defined at no level:

```python
await SurrealDBConnectionManager.signin(username="root", password="root")            # root
await SurrealDBConnectionManager.signin(username="u", password="p", namespace="ns")  # NS user
```

Read back who is signed in, optionally hydrated into a model:

```python
me = await SurrealDBConnectionManager.info()                  # dict | None
me = await SurrealDBConnectionManager.info(return_type=User)  # User | None
```

Restore an identity on a later request, and log out:

```python
await SurrealDBConnectionManager.authenticate(stored_jwt)
await SurrealDBConnectionManager.invalidate()   # back to the credentials of set_connection()
```

The identity **survives reconnects**: `get_client()` replays the stored token, so a dropped
connection or a new event loop comes back as the same user rather than silently reverting to
the configured root identity.

Every failure — wrong password, unknown access method, malformed or expired token — raises
`SurrealDbAuthenticationError` on **both** DB lines (it subclasses `SurrealDbError`). Match on
the type, never on the server's wording, which differs per line.

> ⚠️ **Auth changes the identity of the whole connection.** The manager caches one client per
> event loop and every model shares it, so an auth call affects every subsequent ORM operation
> — not just the caller's. An application serving concurrent users must not route them all
> through a single connection-manager identity.
>
> ⚠️ **`info()` returns `None` unless the table grants the record user `select` on itself.**
> The signin succeeded and `$auth` is set, but the server returns nothing. Grant something like
> `PERMISSIONS FOR select WHERE id = $auth.id`.

**Refresh tokens (SurrealDB 3.x only).** With `DEFINE ACCESS … WITH REFRESH`, renew without the
password:

```python
tokens = await SurrealDBConnectionManager.signin(access="account", refresh=stored_refresh)
persist(tokens.refresh)   # REQUIRED — see below
```

> ⚠️ **Refresh tokens rotate.** A successful exchange kills the token it spent, immediately and
> permanently. If you keep the old value and drop the new one, the user is logged out for good
> — and nothing is raised at the moment you make the mistake.

## 19. Model-level authentication (`AuthenticatedUserMixin`)

Where section 18 authenticates the _connection_, this authenticates a **model**: declare a user
model, let the ORM generate its `DEFINE ACCESS` statement, and get **hydrated model instances**
back instead of bare tokens.

```python
from surreal_orm_lite import AuthenticatedUserMixin, BaseSurrealModel, SurrealConfigDict

class User(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(access_name="account")

    id: str | None = None
    email: str
    password: str
    name: str = ""

# Once, at start-up — emits DEFINE TABLE … PERMISSIONS + DEFINE ACCESS …
await User.define_access()

result = await User.signup(email="ada@example.com", password="s3cret", name="Ada")
result.user     # a User instance, carrying the id the server generated
result.tokens   # AuthTokens — repr redacts, so no JWT reaches your logs

result = await User.signin(email="ada@example.com", password="s3cret")

# A later request carrying the stored JWT: token → current user
me = await User.authenticate(result.tokens.access)
```

**Each call runs on its own short-lived connection.** The process-wide client keeps the identity
`set_connection()` gave it, so a web application can authenticate concurrent users without them
trampling each other — the footgun that makes section 18's `signin()` unsuitable per-request.
Pass `bind=True` to _also_ adopt the token process-wide (handy in a script or notebook, wrong in
a concurrent server).

The model must declare either an `id` field or a `primary_key`, as every ORM model must. Prefer
`id` here so the server mints the record id during SIGNUP; configure `primary_key` when identity
_is_ a column, and the SIGNUP clause sets the record id from it to match
(`CREATE User SET id = $email, email = $email, …`): the same statement on SurrealDB 2.x, 3.x and the embedded engine,
and the record id is the key on all of them. A key that is `None`, or a float that is not an integer in
[-2\*\*63, 2\*\*63) (`1.5`, NaN, inf, `1e19`: SurrealDB 2.x would truncate `1.5` to `<table>:1` and saturate `1e19` to
`<table>:9223372036854775807`, 3.x refuses both), makes `signup()` raise `ValueError` before the exchange; an integral
float within that range, such as `2.0`, creates `<table>:2`.

### Configuration

| Key                     | Default          | Purpose                                      |
| ----------------------- | ---------------- | -------------------------------------------- |
| `access_name`           | `<table>_access` | Name of the `DEFINE ACCESS` method           |
| `identifier_field`      | `email`          | Field a user signs in with                   |
| `password_field`        | `password`       | Field holding the password hash              |
| `auth_algorithm`        | `argon2`         | `argon2` / `bcrypt` / `pbkdf2` / `scrypt`    |
| `auth_duration_token`   | `15m`            | JWT lifetime                                 |
| `auth_duration_session` | `12h`            | Session lifetime                             |
| `with_refresh`          | `False`          | Emit `WITH REFRESH` — **SurrealDB 3.x only** |
| `auth_duration_grant`   | `30d`            | Refresh-grant lifetime                       |

### Inspecting the DDL before applying it

`access_ddl()` is pure — it renders without touching the database, so you can print it, diff it,
or feed it to a migration:

```python
for statement in User.access_ddl():
    print(statement)
# DEFINE TABLE OVERWRITE User SCHEMALESS PERMISSIONS FOR select, update WHERE id = $auth.id;
# DEFINE ACCESS OVERWRITE account ON DATABASE TYPE RECORD SIGNUP ( ... ) SIGNIN ( ... ) ...;
```

The `DEFINE TABLE` ships **by default**, and deliberately: without `FOR select WHERE id = $auth.id`
a signin succeeds and `$auth` is set, yet the server returns no record — so no instance can be
built. `define_access(with_table=False)` opts out when you manage the table yourself.
Since v0.34.0, a user model whose schema mode is `SCHEMAFULL` — `table_type=TableType.USER` (the full ORM's key for an
auth table) or `schema_mode=SchemaMode.SCHEMAFULL` — gets from `define_access()` a `SCHEMAFULL` table with one
`DEFINE FIELD` per field (section 35); every other model keeps the `SCHEMALESS` table printed above. `define_table()`
gives such a model the same table, so the two can be called in any order.

> **The password field holds the hash.** SurrealDB returns the stored record, so
> `result.user.password` is `$argon2id$v=19$…`, never the plaintext you submitted.

**Keep the password field in `model_dump()`.** SIGNUP writes every field, while `update()` and `upsert()` replace the
record with what `model_dump()` writes: a field declared `Field(exclude=True)` (`password: str = Field(exclude=True)`)
would lose its stored value, the password hash, and every later signin would fail. Such a model raises `TypeError`,
naming the fields, before any request and whatever its schema mode: `access_ddl()`, `define_access()` (with or without
the table), `table_ddl()`, `define_table()`, `signup()`, `signin()`, `authenticate()` and `refresh()` refuse it, and so
do `update()` and `upsert()` (with or without `tx=`, before any signal), whoever wrote the record — another client, an
access defined by hand, a session opened before the upgrade. Remove `exclude=True` and leave the field out where you
serialise the model instead (`model_dump(exclude={"password"})`). **Changed in v0.35.4**: `authenticate()`, `refresh()`,
`update()` and `upsert()` used to accept such a model, so a record loaded with `authenticate()` or `get()` lost its
hash at its first `update()` or `upsert()`.

### Renewing a session (SurrealDB 3.x only)

```python
class User(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(access_name="account", with_refresh=True)
    ...

result = await User.refresh(stored_refresh_token)
persist(result.tokens.refresh)   # REQUIRED — refresh tokens rotate
```

On SurrealDB 2.6.x the `WITH REFRESH` clause does not parse at all, so `define_access()` raises a
`SurrealDbError` that says exactly that, and no refresh token ever exists.
