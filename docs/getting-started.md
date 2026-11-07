# Getting started

Why the project exists, what it needs, how to install it and a first model end to end.

## Why This Project?

This ORM is designed to:

- Use the **official SurrealDB SDK** (`surrealdb[pydantic]>=3.0.0,<4.0.0`) for maximum compatibility
- Stay **lightweight** with minimal dependencies
- Keep **up-to-date** with SurrealDB and SDK releases
- Provide **Django-style** query syntax that developers love

## Requirements

| Dependency   | Version                                                                                   |
| ------------ | ----------------------------------------------------------------------------------------- |
| Python       | 3.11+ (tested on 3.11 to 3.15)                                                            |
| SurrealDB    | 2.x / 3.x / 4.x (2.6, 2.7, 3.2, 3.3, 4.0), or the SDK's embedded engine with `[embedded]` |
| Official SDK | surrealdb[pydantic]>=3.0.0,<4.0.0                                                         |
| Pydantic     | >=2.13.4                                                                                  |

> **Note**: Since v0.50.0, Surreal ORM Lite targets the official SurrealDB Python SDK 3.0 (`surrealdb[pydantic]>=3.0.0,<4.0.0`) and supports the SurrealDB versions it supports. It is tested against SurrealDB **v2.7.0**, **v3.3.5** and **v4.0.0**; SurrealDB 3.1.5 is run as a backward-compatibility check, but a regression there does not block a release. The SDK 2.0 line (`0.40.x`) is deprecated and receives security fixes only: [Migrating to 0.50](migrating-to-0.50.md) lists what changes, and `pip install "surreal-orm-lite<0.50"` keeps SDK 2.0.

## Installation

```bash
pip install surreal-orm-lite
```

Or with uv:

```bash
uv add surreal-orm-lite
```

The SDK's in-process engine (`mem://`, `surrealkv://`) and the testing helpers need the `embedded` extra:
`pip install "surreal-orm-lite[embedded]"`.

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
