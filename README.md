# Surreal ORM Lite

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![SurrealDB](https://img.shields.io/badge/SurrealDB-2.6.0-purple)
![SDK](https://img.shields.io/badge/SDK-Official%201.0.8-green)
![License](https://img.shields.io/badge/license-MIT-blue)

**Surreal ORM Lite** is a lightweight, Django-style ORM for [SurrealDB](https://surrealdb.com/) that uses the **official SurrealDB Python SDK**. It provides a simple and intuitive interface for database operations with full async support and Pydantic validation.

## Why This Project?

This ORM is designed to:

- Use the **official SurrealDB SDK** (`surrealdb>=1.0.8`) for maximum compatibility
- Stay **lightweight** with minimal dependencies
- Keep **up-to-date** with SurrealDB and SDK releases
- Provide **Django-style** query syntax that developers love

---

## Requirements

| Dependency   | Version          |
| ------------ | ---------------- |
| Python       | 3.11+            |
| SurrealDB    | 2.6.0+           |
| Official SDK | surrealdb>=1.0.8 |
| Pydantic     | >=2.12.5         |

---

## Installation

```bash
pip install surreal-orm-lite
```

Or with uv:

```bash
uv add surreal-orm-lite
```

---

## Quick Start

### 1. Configure the Connection

```python
from surreal_orm import SurrealDBConnectionManager

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
from surreal_orm import BaseSurrealModel
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

### 4. QuerySet Methods

```python
# Filter with Django-style lookups
users = await User.objects().filter(
    age__gte=18,
    name__startswith="A"
).exec()

# Ordering
users = await User.objects().order_by("name").exec()
users = await User.objects().order_by("age", OrderBy.DESC).exec()

# Pagination
users = await User.objects().limit(10).offset(20).exec()

# Select specific fields
results = await User.objects().select("name", "email").exec()

# Get first result
user = await User.objects().filter(name="Alice").first()

# Get all records
all_users = await User.objects().all()

# Custom query
results = await User.objects().query(
    "SELECT * FROM User WHERE age > $min_age",
    {"min_age": 21}
)
```

---

## Features

| Feature               | Status |
| --------------------- | ------ |
| Async/await support   | ✅     |
| Pydantic validation   | ✅     |
| CRUD operations       | ✅     |
| QuerySet with filters | ✅     |
| Django-style lookups  | ✅     |
| Custom primary keys   | ✅     |
| HTTP connections      | ✅     |
| WebSocket connections | ✅     |

### Supported Filter Lookups

- `exact` (default)
- `gt`, `gte`, `lt`, `lte`
- `in`
- `contains`, `icontains`
- `startswith`, `istartswith`
- `endswith`, `iendswith`

---

## Configuration Options

### Custom Primary Key

```python
from surreal_orm import BaseSurrealModel, SurrealConfigDict

class Product(BaseSurrealModel):
    model_config = SurrealConfigDict(primary_key="sku")

    sku: str
    name: str
    price: float
```

### Context Manager

```python
async with SurrealDBConnectionManager():
    users = await User.objects().all()
# Connection automatically closed
```

---

## Compatibility

This ORM is tested and compatible with:

| SurrealDB Version | SDK Version | Status        |
| ----------------- | ----------- | ------------- |
| 2.6.0             | 1.0.8       | ✅ Tested     |
| 2.5.x             | 1.0.8       | ✅ Compatible |

---

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m "Add amazing feature"`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## Advanced Features?

This project prioritizes **stability and compatibility** with the official SurrealDB Python SDK. Due to current SDK limitations, some advanced features cannot be implemented here.

For a feature-rich ORM with relations, transactions, and more, see:

- **GitHub**: [SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM/)
- **PyPI**: [surrealdb-orm](https://pypi.org/project/surrealdb-orm/)

When the official SDK supports additional features, they will be incorporated into this lite version.

---

## License

MIT License - see [LICENSE](LICENSE) for details.

---

## Author

**Yannick Croteau**
GitHub: [@EulogySnowfall](https://github.com/EulogySnowfall)

---

## Related Projects

- [SurrealDB](https://surrealdb.com/) - The database
- [surrealdb.py](https://github.com/surrealdb/surrealdb.py) - Official Python SDK
