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
| v0.4.0  | Model Signals                      | 📋 Planifié  |
| v0.5.0  | Bulk Operations                    | 📋 Planifié  |
| v0.6.0  | Relations & Graph                  | 📋 Planifié  |
| v0.7.0  | Transactions ORM                   | 📋 Planifié  |
| v1.0.0  | Production Ready                   | 📋 Planifié  |

---

## Version 0.3.0 - Aggregations & Utilities

**Objectif**: Ajouter les fonctions d'agrégation et améliorer l'API QuerySet

### Features

#### 1. Aggregations (Priorité: Haute)

```python
# API proposée
count = await User.objects().count()
count = await User.objects().filter(status="active").count()

total = await Order.objects().sum("amount")
avg_age = await User.objects().avg("age")
max_price = await Product.objects().max("price")
min_price = await Product.objects().min("price")
```

**Fichiers à créer/modifier**:

- [ ] `src/surreal_orm_lite/aggregations.py` - Classes Count, Sum, Avg, Min, Max
- [ ] `src/surreal_orm_lite/query_set.py` - Méthodes count(), sum(), avg(), min(), max()
- [ ] `tests/test_aggregations.py` - Tests unitaires et e2e

**Implémentation technique**:

```python
# Génère: SELECT count() FROM User GROUP ALL
# Génère: SELECT math::sum(amount) FROM Order GROUP ALL
```

#### 2. GROUP BY avec values() et annotate() (Priorité: Moyenne)

```python
# Grouper par status et compter
results = await User.objects().values("status").annotate(count=Count()).exec()
# [{"status": "active", "count": 42}, {"status": "inactive", "count": 8}]

# Grouper et calculer des agrégats
results = await Order.objects().values("customer_id").annotate(
    total=Sum("amount"),
    avg_order=Avg("amount")
).exec()
```

**Fichiers à modifier**:

- [ ] `src/surreal_orm_lite/query_set.py` - Méthodes values(), annotate()

#### 3. raw_query() class method (Priorité: Haute)

```python
# Requête SurrealQL directe avec variables
results = await User.raw_query(
    "SELECT * FROM User WHERE age > $min_age AND status = $status",
    variables={"min_age": 18, "status": "active"}
)

# Requêtes complexes
results = await User.raw_query("""
    SELECT *, ->purchased->Product AS products
    FROM User
    WHERE id = $user_id
""", variables={"user_id": "user:123"})
```

**Fichiers à modifier**:

- [ ] `src/surreal_orm_lite/model_base.py` - Ajouter classmethod raw_query()

#### 4. exists() method (Priorité: Basse)

```python
# Vérifier si des enregistrements existent
has_admins = await User.objects().filter(role="admin").exists()
```

**Fichiers à modifier**:

- [ ] `src/surreal_orm_lite/query_set.py` - Méthode exists()

### Critères de complétion v0.3.0

- [ ] Tous les tests passent
- [ ] Coverage >= 70%
- [ ] Documentation mise à jour
- [ ] CHANGELOG mis à jour

---

## Version 0.4.0 - Model Signals

**Objectif**: Système d'événements Django-style pour le cycle de vie des modèles

### Features

#### 1. Pre/Post Signals (Priorité: Haute)

```python
from surreal_orm_lite import pre_save, post_save, pre_delete, post_delete

@post_save.connect(User)
async def on_user_saved(sender, instance, created, **kwargs):
    """Appelé après chaque sauvegarde de User."""
    if created:
        await send_welcome_email(instance.email)
    await invalidate_cache(f"user:{instance.id}")

@pre_delete.connect(User)
async def on_user_deleting(sender, instance, **kwargs):
    """Appelé avant la suppression de User."""
    await archive_user_data(instance.id)

@post_delete.connect(User)
async def on_user_deleted(sender, instance, **kwargs):
    """Appelé après la suppression de User."""
    await cleanup_user_files(instance.id)
```

**Types de signaux**:

| Signal        | Quand          | Arguments                       |
| ------------- | -------------- | ------------------------------- |
| `pre_save`    | Avant save()   | sender, instance                |
| `post_save`   | Après save()   | sender, instance, created       |
| `pre_update`  | Avant update() | sender, instance, update_fields |
| `post_update` | Après update() | sender, instance, update_fields |
| `pre_delete`  | Avant delete() | sender, instance                |
| `post_delete` | Après delete() | sender, instance                |

**Fichiers à créer/modifier**:

- [ ] `src/surreal_orm_lite/signals.py` - Classes Signal, pre_save, post_save, etc.
- [ ] `src/surreal_orm_lite/model_base.py` - Intégrer les signaux dans CRUD
- [ ] `src/surreal_orm_lite/__init__.py` - Exporter les signaux
- [ ] `tests/test_signals.py` - Tests

#### 2. Around Signals (Priorité: Moyenne)

```python
from surreal_orm_lite import around_save

@around_save.connect(User)
async def time_user_save(sender, instance, created, **kwargs):
    """Wrapper autour de save() avec état partagé."""
    import time
    start = time.time()

    yield  # <-- save() s'exécute ici

    duration = time.time() - start
    await log_metric(f"user_save_duration", duration)
```

**Fichiers à créer/modifier**:

- [ ] `src/surreal_orm_lite/signals.py` - AroundSignal class

### Critères de complétion v0.4.0

- [ ] Tous les signaux fonctionnent
- [ ] Tests de régression CRUD
- [ ] Documentation avec exemples

---

## Version 0.5.0 - Bulk Operations

**Objectif**: Opérations en masse performantes

### Features

#### 1. bulk_create() (Priorité: Haute)

```python
users = [
    User(name="Alice", email="alice@example.com"),
    User(name="Bob", email="bob@example.com"),
    User(name="Charlie", email="charlie@example.com"),
]

# Création en masse (une seule requête)
created_users = await User.objects().bulk_create(users)

# Avec option atomique (transaction)
created_users = await User.objects().bulk_create(users, atomic=True)
```

**Implémentation technique**:

```sql
-- Génère une seule requête INSERT
INSERT INTO User [
    { name: "Alice", email: "alice@example.com" },
    { name: "Bob", email: "bob@example.com" },
    { name: "Charlie", email: "charlie@example.com" }
];
```

#### 2. bulk_update() (Priorité: Moyenne)

```python
# Mettre à jour tous les utilisateurs filtrés
await User.objects().filter(status="pending").bulk_update(status="active")

# Avec plusieurs champs
await User.objects().filter(role="guest").bulk_update(
    role="member",
    updated_at=datetime.now()
)
```

**Implémentation technique**:

```sql
-- Génère UPDATE avec WHERE
UPDATE User SET status = "active" WHERE status = "pending";
```

#### 3. bulk_delete() (Priorité: Moyenne)

```python
# Supprimer tous les utilisateurs inactifs
deleted_count = await User.objects().filter(status="inactive").bulk_delete()

# Avec condition complexe
deleted_count = await User.objects().filter(
    last_login__lt=datetime(2024, 1, 1)
).bulk_delete()
```

**Fichiers à créer/modifier**:

- [ ] `src/surreal_orm_lite/query_set.py` - bulk_create(), bulk_update(), bulk_delete()
- [ ] `tests/test_bulk_operations.py` - Tests de performance et atomicité

### Critères de complétion v0.5.0

- [ ] Opérations bulk fonctionnelles
- [ ] Tests de performance (> 100 records)
- [ ] Option atomic testée

---

## Version 0.6.0 - Relations & Graph

**Objectif**: Support basique des relations SurrealDB

### Features

#### 1. relate() et remove_relation() (Priorité: Haute)

```python
# Créer une relation
await user.relate("follows", other_user)
await post.relate("authored_by", user)

# Supprimer une relation
await user.remove_relation("follows", other_user)
await user.remove_relation("follows", "users:other_id")  # Par ID
```

**Implémentation technique**:

```sql
-- RELATE crée un edge dans le graph
RELATE users:alice->follows->users:bob;

-- DELETE supprime la relation
DELETE follows WHERE in = users:alice AND out = users:bob;
```

#### 2. get_related() (Priorité: Haute)

```python
# Récupérer les utilisateurs suivis
following = await user.get_related("follows", direction="out", model_class=User)

# Récupérer les followers
followers = await user.get_related("follows", direction="in", model_class=User)

# Récupérer les posts d'un utilisateur
posts = await user.get_related("authored", direction="out", model_class=Post)
```

**Implémentation technique**:

```sql
-- Direction OUT
SELECT VALUE out.* FROM follows WHERE in = users:alice;

-- Direction IN
SELECT VALUE in.* FROM follows WHERE out = users:alice;
```

#### 3. Graph Traversal basique (Priorité: Moyenne)

```python
# Traverser le graph avec syntaxe SurrealDB
friends_of_friends = await user.traverse("->follows->User->follows->User")

# Avec filtre
active_followers = await user.traverse(
    "<-follows<-User",
    where="status = 'active'"
)
```

#### 4. select_related() / prefetch_related() (Priorité: Basse)

```python
# Charger les relations en une seule requête (FETCH)
users = await User.objects().select_related("profile", "settings").exec()

# Précharger pour éviter N+1
posts = await Post.objects().prefetch_related("author", "comments").exec()
```

**Implémentation technique**:

```sql
-- Utilise FETCH de SurrealDB
SELECT *, author.* AS author FROM posts FETCH author;
```

**Fichiers à créer/modifier**:

- [ ] `src/surreal_orm_lite/relations.py` - RelationManager, RelationQuerySet
- [ ] `src/surreal_orm_lite/model_base.py` - relate(), remove_relation(), get_related()
- [ ] `src/surreal_orm_lite/query_set.py` - select_related(), prefetch_related()
- [ ] `tests/test_relations.py` - Tests

### Critères de complétion v0.6.0

- [ ] Relations CRUD fonctionnelles
- [ ] Graph traversal basique
- [ ] Tests avec modèles liés

---

## Version 0.7.0 - Transactions ORM

**Objectif**: Support des transactions au niveau ORM

### Features

#### 1. Transaction Context Manager (Priorité: Haute)

```python
from surreal_orm_lite import transaction

async with transaction() as tx:
    user = User(name="Alice", email="alice@example.com")
    await user.save(tx=tx)

    order = Order(user_id=user.id, total=100)
    await order.save(tx=tx)

    # Auto-commit si pas d'exception
    # Auto-rollback si exception

# Équivalent avec Model.transaction()
async with User.transaction() as tx:
    await user.save(tx=tx)
    await order.save(tx=tx)
```

#### 2. Paramètre tx= sur toutes les opérations (Priorité: Haute)

```python
async with transaction() as tx:
    # CRUD avec transaction
    await user.save(tx=tx)
    await user.update(tx=tx)
    await user.merge(age=30, tx=tx)
    await user.delete(tx=tx)

    # QuerySet avec transaction
    users = await User.objects(tx=tx).filter(status="active").exec()
    await User.objects(tx=tx).filter(status="old").bulk_delete()
```

**Fichiers à créer/modifier**:

- [ ] `src/surreal_orm_lite/transaction.py` - Transaction context manager
- [ ] `src/surreal_orm_lite/model_base.py` - Paramètre tx= sur CRUD
- [ ] `src/surreal_orm_lite/query_set.py` - Paramètre tx= sur QuerySet
- [ ] `tests/test_transactions.py` - Tests atomicité

### Note sur le SDK officiel

Le support des transactions dépend des capacités du SDK officiel `surrealdb>=1.0.8`.
Si le SDK ne supporte pas les transactions, cette feature sera reportée ou implémentée
via des requêtes SurrealQL `BEGIN TRANSACTION` / `COMMIT` / `CANCEL`.

---

## Version 1.0.0 - Production Ready

**Objectif**: Version stable pour production

### Features

#### 1. Améliorations de stabilité

- [ ] Gestion robuste des erreurs
- [ ] Retry automatique sur déconnexion
- [ ] Logging configurable
- [ ] Métriques de performance

#### 2. Configuration avancée

```python
class User(BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_name="users",           # Nom de table custom
        primary_key="email",          # Clé primaire custom
        server_fields=["created_at", "updated_at"],  # Champs serveur
        strict_mode=True,             # Validation stricte
    )
```

#### 3. Field Aliases (Priorité: Moyenne)

```python
from pydantic import Field

class User(BaseSurrealModel):
    # Python 'password' -> DB 'password_hash'
    password: str = Field(alias="password_hash")
```

#### 4. Documentation complète

- [ ] Docstrings sur toutes les classes/méthodes publiques
- [ ] README avec tous les exemples
- [ ] Changelog complet
- [ ] Guide de migration depuis v0.x

### Critères de complétion v1.0.0

- [ ] Coverage >= 80%
- [ ] Tous les tests e2e passent
- [ ] Documentation complète
- [ ] Pas de breaking changes depuis v0.7.0
- [ ] Performance benchmarks documentés

---

## Features hors scope (SDK custom requis)

Ces features ne peuvent pas être implémentées avec le SDK officiel actuel:

| Feature            | Raison                             |
| ------------------ | ---------------------------------- |
| CBOR Protocol      | SDK officiel utilise JSON          |
| LiveSelectStream   | Nécessite gestion WebSocket custom |
| Auto-Resubscribe   | Gestion reconnexion WebSocket      |
| Connection Pool    | Pas dans le SDK officiel           |
| Change Feeds (CDC) | Pas supporté par le SDK            |

Ces features sont disponibles dans [SurrealDB-ORM](https://github.com/EulogySnowfall/SurrealDB-ORM).

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
