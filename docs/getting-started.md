# Getting started

Why the project exists, what it needs, how to install it and a first model end to end.

## Why This Project?

This ORM is designed to:

- Use the **official SurrealDB SDK** (`surrealdb[pydantic]>=2.0.0,<3.0.0`) for maximum compatibility
- Stay **lightweight** with minimal dependencies
- Keep **up-to-date** with SurrealDB and SDK releases
- Provide **Django-style** query syntax that developers love

## Requirements

| Dependency   | Version                                                      |
| ------------ | ------------------------------------------------------------ |
| Python       | 3.11+ (tested on 3.11 to 3.15)                               |
| SurrealDB    | 2.6.x / 2.7.x or 3.2.x / 3.3.x, or the SDK's embedded engine |
| Official SDK | surrealdb[pydantic]>=2.0.0,<3.0.0                            |
| Pydantic     | >=2.13.4                                                     |

> **Note**: As of v0.7.0, Surreal ORM Lite targets the SurrealDB Python SDK 2.x (`surrealdb[pydantic]>=2.0.0,<3.0.0`), which supports the SurrealDB 3.x protocol. It is tested against SurrealDB **v2.7.0** and **v3.3.5**; up to v0.19.0 the pins were v2.6.5 and v3.2.4. The 2.x line behaves identically on 2.7.0, and 3.3.0 differs from 3.2.4 in one server behaviour, listed in the behaviour table. SurrealDB 3.1.x is no longer a supported line as of v0.14.0 — the suite is still run against 3.1.5 as a backward-compatibility check, but a regression there does not block a release.

## Installation

```bash
pip install surreal-orm-lite
```

Or with uv:

```bash
uv add surreal-orm-lite
```

## Quick Start

### 1. Configure the Connection

```python
from surreal_orm_lite import SurrealDBConnectionManager

SurrealDBConnectionManager.set_connection(
    url="http://localhost:8000",
    user="root",
    password="root",
    namespace="my_namespace",
    database="my_database",
)
```

### 2. Define a Model

```python
from surreal_orm_lite import BaseSurrealModel
from pydantic import Field

class User(BaseSurrealModel):
    id: str | None = None
    name: str = Field(..., max_length=100)
    email: str
    age: int = Field(..., ge=0)
```

### 3. CRUD Operations

```python
# Create
user = User(name="Alice", email="alice@example.com", age=30)
await user.save()

# Read
user = await User.objects().get("alice_id")
users = await User.objects().filter(age__gte=18).exec()

# Update
user.age = 31
await user.update()

# Or partial update
await user.merge(age=31)

# Delete
await user.delete()
```
