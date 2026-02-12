# SurrealDB-ORM-lite Roadmap

> Roadmap d'implémentation des features inspirées de [SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM)
>
> **Contrainte principale**: Toutes les features doivent être compatibles avec le SDK officiel `surrealdb>=1.0.8`

---

## Vue d'ensemble

| Version | Thème                              | Statut       |
| ------- | ---------------------------------- | ------------ |
| v0.2.x  | Core ORM (CRUD, QuerySet, Filters) | ✅ Complété  |
| v0.3.0  | Aggregations & Utilities           | ✅ Complété  |
| v0.4.0  | Model Signals                      | ✅ Complété  |
| v0.5.0  | Bulk Operations & Q Objects        | 📋 Planifié  |
| v0.6.0  | Relations & Graph                  | 📋 Planifié  |
| v0.7.0  | Transactions ORM                   | 📋 Planifié  |
| v0.8.0  | SurrealFunc & Computed Fields      | 📋 Planifié  |
| v0.9.0  | FETCH, Field Aliases & DX          | 📋 Planifié  |
| v1.0.0  | Production Ready                   | 📋 Planifié  |

---

## Comparaison SurrealDB-ORM vs SurrealDB-ORM-lite

### Features implémentables dans Lite (SDK officiel)

| Feature                       | ORM (full) | ORM-lite   | Version cible |
| ----------------------------- | ---------- | ---------- | ------------- |
| CRUD (save, update, merge)    | ✅ v0.2+   | ✅ v0.2.0  | -             |
| QuerySet & Filters            | ✅ v0.2+   | ✅ v0.2.0  | -             |
| Aggregations & GROUP BY       | ✅ v0.3+   | ✅ v0.3.0  | -             |
| raw_query()                   | ✅ v0.5.4  | ✅ v0.3.0  | -             |
| Model Signals (pre/post)      | ✅ v0.5.7  | ✅ v0.4.0  | -             |
| Around Signals                | ✅ v0.5.8  | ✅ v0.4.0  | -             |
| Bulk Operations               | ✅ v0.4+   | ❌         | v0.5.0        |
| Q Objects (OR/AND/NOT)        | ✅ v0.6.0  | ❌         | v0.5.0        |
| Lookups supplémentaires       | ✅ v0.5.9  | ❌         | v0.5.0        |
| Filtres paramétrés ($vars)    | ✅ v0.6.0  | ❌         | v0.5.0        |
| `-field` ordering (DESC)      | ✅ v0.6.0  | ❌         | v0.5.0        |
| Relations & Graph (relate)    | ✅ v0.4.0  | ❌         | v0.6.0        |
| get_related() / traverse()    | ✅ v0.4.0  | ❌         | v0.6.0        |
| FETCH clause                  | ✅ v0.7.0  | ❌         | v0.6.0        |
| remove_all_relations()        | ✅ v0.6.0  | ❌         | v0.6.0        |
| Transactions ORM (tx=)        | ✅ v0.6+   | ❌         | v0.7.0        |
| SurrealFunc (time::now())     | ✅ v0.6.0  | ❌         | v0.8.0        |
| Computed Fields               | ✅ v0.8.0  | ❌         | v0.8.0        |
| server_values sur save()      | ✅ v0.7.0  | ❌         | v0.8.0        |
| Field Aliases                 | ✅ v0.5.5  | ❌         | v0.9.0        |
| call_function()               | ✅ v0.7.0  | ❌         | v0.9.0        |
| Retry, Logging, Métriques     | ✅ v0.7+   | ❌         | v1.0.0        |

### Features exclusives à SurrealDB-ORM (SDK custom requis)

Ces features ne peuvent **pas** être implémentées avec le SDK officiel actuel et resteront exclusives au ORM complet:

| Feature                          | ORM (full) | Raison                                  |
| -------------------------------- | ---------- | --------------------------------------- |
| SDK custom (`surreal_sdk`)       | ✅         | Architecture fondamentale               |
| CBOR Protocol                    | ✅ v0.5.5  | SDK officiel gère en interne            |
| Connection Pool                  | ✅ v0.5+   | Pas dans le SDK officiel                |
| Live Models (ORM-level)         | ✅ v0.9.0  | Nécessite gestion WebSocket avancée     |
| Auto-Resubscribe                 | ✅ v0.9.0  | Gestion reconnexion WebSocket custom    |
| Change Feeds (CDC)               | ✅ v0.9.0  | Nécessite polling + cursor tracking     |
| Schema Introspection             | ✅ v0.10.0 | Système complet inspectdb/schemadiff    |
| Multi-Database Support           | ✅ v0.10.0 | Registre de connexions nommées          |
| Migrations (makemigrations)      | ✅ v0.10.0 | Système Django-style complet            |
| CLI (`surreal-orm` commands)     | ✅ v0.10.0 | Shell, migrate, inspectdb               |
| Subqueries                       | ✅ v0.11.0 | QuerySet imbriqués                      |
| Query Cache (TTL)                | ✅ v0.11.0 | Cache avec invalidation automatique     |
| Vector Search (KNN/HNSW)        | ✅ v0.12.0 | VectorField + similar_to()              |
| Full-Text Search (BM25)         | ✅ v0.12.0 | search() + SearchScore/Highlight        |
| Hybrid Search (RRF)             | ✅ v0.12.0 | Combinaison vector + FTS               |
| DEFINE EVENT                     | ✅ v0.13.0 | Triggers serveur dans migrations        |
| Geospatial Fields                | ✅ v0.13.0 | PointField, PolygonField, nearby()      |
| Materialized Views               | ✅ v0.13.0 | DEFINE TABLE ... AS SELECT              |
| TYPE RELATION                    | ✅ v0.13.0 | Contraintes graph dans migrations       |
| JWT Authentication               | ✅ v0.8.0  | AuthenticatedUserMixin, signup/signin   |
| Test Fixtures & Factories        | ✅ v0.14.0 | SurrealFixture, ModelFactory            |
| QueryLogger (debug)              | ✅ v0.14.0 | Profiling des requêtes ORM              |
| Atomic Array Operations          | ✅ v0.5.9  | atomic_append/remove/set_add            |
| Retry on Conflict                | ✅ v0.5.9  | retry_on_conflict() decorator           |

---

## Versions complétées

### Version 0.2.x - Core ORM ✅

- Django-style ORM avec `BaseSurrealModel`
- `QuerySet` avec fluent builder
- CRUD: `save()`, `update()`, `merge()`, `delete()`, `refresh()`
- Filter lookups: `exact`, `gt`, `gte`, `lt`, `lte`, `in`, `contains`, `icontains`, `startswith`, `istartswith`, `endswith`, `iendswith`, `like`, `ilike`, `match`, `regex`, `iregex`, `isnull`
- `SurrealDBConnectionManager` (HTTP + WebSocket)
- Custom primary keys via `SurrealConfigDict`
- Validation Pydantic 2.x
- Coverage 97%+

### Version 0.3.0 - Aggregations & Utilities ✅

- Classes d'agrégation: `Count`, `Sum`, `Avg`, `Min`, `Max`
- Méthodes QuerySet: `count()`, `sum()`, `avg()`, `min()`, `max()`
- GROUP BY: `values()` + `annotate()`
- `exists()` pour vérifier l'existence
- `raw_query()` class method pour SurrealQL arbitraire
- Coverage 94%+

### Version 0.4.0 - Model Signals ✅

- `Signal` class: `pre_save`, `post_save`, `pre_update`, `post_update`, `pre_delete`, `post_delete`
- `AroundSignal` class: `around_save`, `around_update`, `around_delete`
- `connect()` / `disconnect()` / `clear()` / `has_handlers()`
- Flag `created` sur `post_save`
- `update_fields` sur `pre_update` / `post_update`

---

## Version 0.5.0 - Bulk Operations & Q Objects

**Objectif**: Opérations en masse et requêtes complexes

### Features

#### 1. Q Objects pour requêtes complexes (Priorité: Haute)

Support des opérateurs OR, AND, NOT pour combiner des filtres de manière flexible.

```python
from surreal_orm_lite import Q

# OR query
users = await User.objects().filter(
    Q(name__contains="alice") | Q(email__contains="alice"),
).exec()

# NOT query
users = await User.objects().filter(
    ~Q(status="banned"),
    role="admin",
).exec()

# AND combiné avec OR
users = await User.objects().filter(
    Q(age__gte=18) & Q(age__lte=65),
    Q(role="admin") | Q(role="moderator"),
).exec()
```

**Implémentation technique**:

```python
class Q:
    """Composable query expression."""
    AND = "AND"
    OR = "OR"

    def __init__(self, **kwargs):
        self.filters = kwargs
        self.children = []
        self.connector = self.AND
        self.negated = False

    def __or__(self, other): ...
    def __and__(self, other): ...
    def __invert__(self): ...

    def to_sql(self, table_name: str) -> tuple[str, dict]:
        """Génère la clause WHERE avec variables paramétrées."""
```

**Fichiers à créer/modifier**:

- [ ] `src/surreal_orm_lite/q.py` - Classe Q
- [ ] `src/surreal_orm_lite/query_set.py` - Support Q dans filter()
- [ ] `src/surreal_orm_lite/__init__.py` - Exporter Q
- [ ] `tests/test_q_objects.py` - Tests

#### 2. Filtres paramétrés - Sécurité (Priorité: Haute)

Toutes les valeurs de filtre sont maintenant des variables paramétrées (`$_fN`) au lieu d'être
interpolées dans la requête. Prévient l'injection SQL.

```python
# Avant (interpolation directe - risque injection)
# SELECT * FROM User WHERE name = 'Alice'

# Après (variables paramétrées)
# SELECT * FROM User WHERE name = $_f0  {_f0: "Alice"}
```

**Fichiers à modifier**:

- [ ] `src/surreal_orm_lite/query_set.py` - Refactorer _build_where() pour utiliser des variables
- [ ] `tests/test_e2e.py` - Vérifier que les filtres paramétrés fonctionnent

#### 3. bulk_create() (Priorité: Haute)

```python
users = [
    User(name="Alice", email="alice@example.com"),
    User(name="Bob", email="bob@example.com"),
    User(name="Charlie", email="charlie@example.com"),
]

# Création en masse (une seule requête INSERT)
created_users = await User.objects().bulk_create(users)
```

**Implémentation technique**:

```sql
-- Utilise INSERT INTO (supporté par le SDK via query())
INSERT INTO User [
    { name: "Alice", email: "alice@example.com" },
    { name: "Bob", email: "bob@example.com" },
    { name: "Charlie", email: "charlie@example.com" }
];
```

**Fichiers à modifier**:

- [ ] `src/surreal_orm_lite/query_set.py` - Méthode bulk_create()
- [ ] `tests/test_bulk.py` - Tests

#### 4. bulk_update() (Priorité: Moyenne)

```python
# Mettre à jour tous les utilisateurs filtrés
count = await User.objects().filter(status="pending").bulk_update(status="active")

# Avec plusieurs champs
count = await User.objects().filter(role="guest").bulk_update(
    role="member",
    updated_at=datetime.now()
)
```

**Implémentation technique**:

```sql
-- Génère UPDATE avec WHERE
UPDATE User SET status = $_v0 WHERE status = $_f0;
```

**Fichiers à modifier**:

- [ ] `src/surreal_orm_lite/query_set.py` - Méthode bulk_update()
- [ ] `tests/test_bulk.py` - Tests

#### 5. bulk_delete() (Priorité: Moyenne)

```python
# Supprimer tous les utilisateurs filtrés
deleted_count = await User.objects().filter(status="inactive").bulk_delete()
```

**Implémentation technique**:

```sql
-- Génère DELETE avec WHERE
DELETE User WHERE status = $_f0;
```

**Fichiers à modifier**:

- [ ] `src/surreal_orm_lite/query_set.py` - Méthode bulk_delete()
- [ ] `tests/test_bulk.py` - Tests

#### 6. Lookups supplémentaires (Priorité: Moyenne)

Nouveaux opérateurs de filtre pour compléter la couverture.

```python
# NOT IN
users = await User.objects().filter(status__not_in=["banned", "deleted"]).exec()

# CONTAINSNOT
events = await Event.objects().filter(tags__not_contains="spam").exec()

# CONTAINSALL
posts = await Post.objects().filter(tags__containsall=["python", "surreal"]).exec()

# CONTAINSANY
posts = await Post.objects().filter(tags__containsany=["python", "rust"]).exec()
```

**Fichiers à modifier**:

- [ ] `src/surreal_orm_lite/constants.py` - Ajouter les opérateurs
- [ ] `tests/test_e2e.py` - Tests des nouveaux lookups

#### 7. `-field` ordering shorthand (Priorité: Basse)

```python
# Raccourci pour DESC ordering
users = await User.objects().order_by("-created_at").exec()

# Équivalent à
users = await User.objects().order_by("created_at", OrderBy.DESC).exec()

# Multiples colonnes
users = await User.objects().order_by("-age", "name").exec()
```

**Fichiers à modifier**:

- [ ] `src/surreal_orm_lite/query_set.py` - Améliorer order_by()
- [ ] `tests/test_e2e.py` - Tests

### Critères de complétion v0.5.0

- [ ] Q Objects fonctionnels avec OR/AND/NOT
- [ ] Filtres paramétrés sur tous les QuerySet
- [ ] Opérations bulk (create, update, delete) fonctionnelles
- [ ] Nouveaux lookups (not_in, not_contains, containsall, containsany)
- [ ] `-field` ordering shorthand
- [ ] Tests de performance bulk (> 100 records)
- [ ] Coverage >= 70%

---

## Version 0.6.0 - Relations & Graph

**Objectif**: Support des relations SurrealDB et traversée de graph

### Features

#### 1. relate() et remove_relation() (Priorité: Haute)

```python
# Créer une relation
await user.relate("follows", other_user)
await post.relate("authored_by", user)

# Avec données sur la relation
await user.relate("purchased", product, data={"quantity": 2, "price": 29.99})

# Supprimer une relation
await user.remove_relation("follows", other_user)
await user.remove_relation("follows", "users:other_id")  # Par ID

# Supprimer toutes les relations d'un type
await user.remove_all_relations("follows", direction="out")
```

**Implémentation technique**:

```sql
RELATE users:alice->follows->users:bob;
RELATE users:alice->purchased->products:widget SET quantity = 2, price = 29.99;
DELETE follows WHERE in = users:alice AND out = users:bob;
DELETE follows WHERE in = users:alice;
```

#### 2. get_related() (Priorité: Haute)

```python
# Récupérer les utilisateurs suivis
following = await user.get_related("follows", direction="out", model_class=User)

# Récupérer les followers
followers = await user.get_related("follows", direction="in", model_class=User)
```

#### 3. FETCH clause (Priorité: Haute)

```python
# Résoudre les record links inline (évite N+1)
posts = await Post.objects().fetch("author", "tags").exec()
# Génère: SELECT * FROM posts FETCH author, tags;
```

#### 4. Graph Traversal basique (Priorité: Moyenne)

```python
friends_of_friends = await user.traverse("->follows->User->follows->User")
```

**Fichiers à créer/modifier**:

- [ ] `src/surreal_orm_lite/model_base.py` - relate(), remove_relation(), get_related(), remove_all_relations()
- [ ] `src/surreal_orm_lite/query_set.py` - fetch()
- [ ] `tests/test_relations.py` - Tests

### Critères de complétion v0.6.0

- [ ] Relations CRUD fonctionnelles
- [ ] FETCH clause
- [ ] Graph traversal basique
- [ ] Tests avec modèles liés

---

## Version 0.7.0 - Transactions ORM

**Objectif**: Support des transactions au niveau ORM

### Features

#### 1. Transaction Context Manager (Priorité: Haute)

```python
from surreal_orm_lite import SurrealDBConnectionManager

async with SurrealDBConnectionManager.transaction() as tx:
    user = User(name="Alice", email="alice@example.com")
    await user.save(tx=tx)

    order = Order(user_id=user.id, total=100)
    await order.save(tx=tx)
    # Auto-commit si pas d'exception
    # Auto-rollback si exception
```

#### 2. Paramètre tx= sur toutes les opérations (Priorité: Haute)

```python
async with SurrealDBConnectionManager.transaction() as tx:
    await user.save(tx=tx)
    await user.merge(age=30, tx=tx)
    await user.delete(tx=tx)
    users = await User.objects(tx=tx).filter(status="active").exec()
```

**Note**: Implémentation via `BEGIN TRANSACTION` / `COMMIT` / `CANCEL` en SurrealQL, compatible avec le SDK officiel via `query()`.

**Fichiers à créer/modifier**:

- [ ] `src/surreal_orm_lite/transaction.py` - Transaction context manager
- [ ] `src/surreal_orm_lite/model_base.py` - Paramètre tx= sur CRUD
- [ ] `src/surreal_orm_lite/query_set.py` - Paramètre tx= sur QuerySet
- [ ] `tests/test_transactions.py` - Tests atomicité

---

## Version 0.8.0 - SurrealFunc & Computed Fields

**Objectif**: Fonctions serveur et champs calculés

### Features

#### 1. SurrealFunc (Priorité: Haute)

```python
from surreal_orm_lite import SurrealFunc

# Utiliser des fonctions SurrealDB dans save/merge
await player.save(server_values={"joined_at": SurrealFunc("time::now()")})
await player.merge(last_ping=SurrealFunc("time::now()"))

# Avec variables supplémentaires
await user.save(
    server_values={"password_hash": SurrealFunc("crypto::argon2::generate($password)")},
    extra_vars={"password": raw_password},
)
```

#### 2. Computed Fields (Priorité: Moyenne)

```python
from surreal_orm_lite import Computed

class User(BaseSurrealModel):
    first_name: str
    last_name: str
    full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")

class Order(BaseSurrealModel):
    items: list[dict]
    subtotal: Computed[float] = Computed("math::sum(items.*.price * items.*.qty)")
```

#### 3. call_function() (Priorité: Moyenne)

```python
# Appeler des fonctions SurrealDB custom
result = await SurrealDBConnectionManager.call_function(
    "acquire_game_lock", params={"table_id": tid, "pod_id": pid},
)
```

**Fichiers à créer/modifier**:

- [ ] `src/surreal_orm_lite/functions.py` - SurrealFunc, Computed, call_function()
- [ ] `src/surreal_orm_lite/model_base.py` - server_values, extra_vars
- [ ] `tests/test_functions.py` - Tests

---

## Version 0.9.0 - FETCH, Field Aliases & DX

**Objectif**: Améliorations de l'expérience développeur

### Features

#### 1. Field Aliases (Priorité: Moyenne)

```python
from pydantic import Field

class User(BaseSurrealModel):
    password: str = Field(alias="password_hash")
```

#### 2. server_fields config (Priorité: Moyenne)

```python
class User(BaseSurrealModel):
    model_config = SurrealConfigDict(
        server_fields=["created_at", "updated_at"],  # Exclus du save()
    )
```

#### 3. merge(refresh=False) (Priorité: Basse)

```python
# Skip le SELECT de rafraîchissement pour fire-and-forget
await user.merge(last_seen=SurrealFunc("time::now()"), refresh=False)
```

---

## Version 1.0.0 - Production Ready

**Objectif**: Version stable pour production

### Features

- [ ] Gestion robuste des erreurs
- [ ] Retry automatique sur déconnexion
- [ ] Logging configurable
- [ ] Métriques de performance
- [ ] Documentation complète (docstrings, README, guide migration)
- [ ] Performance benchmarks

### Critères de complétion v1.0.0

- [ ] Coverage >= 80%
- [ ] Tous les tests e2e passent
- [ ] Documentation complète
- [ ] Pas de breaking changes depuis v0.9.0
- [ ] Performance benchmarks documentés

---

## Features exclusives à SurrealDB-ORM (hors scope)

Ces features ne seront **pas** implémentées dans ORM-lite et nécessitent le [ORM complet](https://github.com/EulogySnowfall/SurrealDB-ORM):

| Feature                          | Raison                                            |
| -------------------------------- | ------------------------------------------------- |
| SDK custom (`surreal_sdk`)       | Architecture fondamentale de l'ORM complet        |
| CBOR Protocol                    | SDK officiel gère le protocole en interne          |
| Connection Pool                  | Non supporté par le SDK officiel                  |
| Live Models (ORM-level)         | Nécessite gestion WebSocket avancée               |
| Auto-Resubscribe                 | Reconnexion WebSocket custom                      |
| Change Feeds (CDC)               | Polling + cursor tracking complexe                |
| Schema Introspection             | Système complet inspectdb/schemadiff              |
| Multi-Database Support           | Registre de connexions nommées + contextvars      |
| Migrations système               | makemigrations/migrate/rollback Django-style      |
| CLI commands                     | Shell interactif, commandes admin                 |
| Subqueries                       | QuerySet imbriqués dans des filtres               |
| Query Cache (TTL)                | Cache avec invalidation automatique               |
| Vector Search (KNN/HNSW)        | VectorField + similar_to() + indexes              |
| Full-Text Search (BM25)         | search() + SearchScore/Highlight                  |
| Hybrid Search (RRF)             | Combinaison vector + FTS                          |
| DEFINE EVENT                     | Triggers serveur dans migrations                  |
| Geospatial Fields                | PointField, PolygonField, nearby()                |
| Materialized Views               | DEFINE TABLE ... AS SELECT (read-only models)     |
| TYPE RELATION enforcement        | Contraintes graph dans migrations                 |
| JWT Authentication               | AuthenticatedUserMixin, signup/signin             |
| Test Fixtures & Factories        | SurrealFixture, ModelFactory, QueryLogger         |
| Atomic Array Operations          | atomic_append/remove/set_add (concurrent safety)  |
| Retry on Conflict                | retry_on_conflict() decorator                     |

---

## Contribuer

1. Choisir une feature dans le roadmap
2. Créer une issue pour discussion
3. Fork et créer une branche `feature/xxx`
4. Implémenter avec tests
5. Soumettre une PR

---

## Références

- [SurrealDB Documentation](https://surrealdb.com/docs)
- [SurrealDB Python SDK](https://surrealdb.com/docs/sdk/python/methods)
- [SurrealDB-ORM (full)](https://github.com/EulogySnowfall/SurrealDB-ORM)
- [Pydantic v2](https://docs.pydantic.dev/latest/)
