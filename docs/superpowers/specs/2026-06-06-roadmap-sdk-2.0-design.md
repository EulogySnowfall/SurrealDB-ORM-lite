# Roadmap SurrealDB-ORM-lite — nouvelles features débloquées par le SDK officiel 2.0

- **Date** : 2026-06-06
- **Statut** : Approuvé (design)
- **Version actuelle du projet** : 0.7.0
- **Contrainte structurante** : toutes les features doivent être réalisables avec le SDK
  officiel `surrealdb[pydantic]>=2.0.0,<3.0.0` (protocole SurrealDB 3.x), **sans SDK custom**.

## Objectif

Étendre la roadmap de `surreal-orm-lite` pour exploiter **tout ce que le SDK officiel 2.0
rend possible** — d'abord les primitives qu'il expose nativement (transactions, `upsert`,
`patch`, auth JWT/scope, live queries, `insert_relation`…), puis tout ce qui est réalisable
via `query()` en SurrealQL (DDL inclus : `DEFINE INDEX`, `DEFINE ANALYZER`, `DEFINE EVENT`,
`DEFINE TABLE … AS SELECT`…).

La **Beta** est planifiée en **v0.40.0** (gel d'API + durcissement), et la **version de
production / GA** en **v2.0.0** pour s'aligner sur la version majeure du SDK officiel.

## Décisions de cadrage (validées avec l'utilisateur)

1. **Périmètre** : tout ce qui est **réalisable sous le SDK officiel 2.0** est mis au plan,
   réparti en 3 paliers de features :
   - **Palier 1 — Cœur « strict »** (v0.8.0 → v0.22.0) : primitives SDK 2.0 natives +
     philosophie « lite ».
   - **Palier 2 — Extended SDK-2.0-natif** (v0.23.0 → v0.29.0) : types riches, géo, embarqué,
     subqueries, cache, multi-DB.
   - **Palier 3 — Advanced** (v0.30.0 → v0.39.0) : recherche (vector/FTS/hybrid), schéma & DDL
     (introspection, `DEFINE EVENT`, vues, `TYPE RELATION`), migrations, CLI, fixtures.
     Ces features ne sont **pas bloquées par le SDK** : ce sont des fonctionnalités SurrealQL
     exécutées via `query()` (DDL + opérateurs). Elles étaient initialement en « Future » non
     par limite SDK mais parce que ce sont de **gros chantiers** ; l'utilisateur a choisi de
     les **engager au plan** (cf. décision validée le 2026-06-06).
2. **Granularité** : **un thème par version mineure**. Les gros thèmes (Transactions, Auth,
   Temps réel) sont **scindés** en mineures dédiées ; les petits thèmes ne sont **pas fusionnés**.
3. **Beta** : **v0.40.0** = feature-complete, durcissement, **gel d'API** avant la GA.
4. **Production / GA** : **v2.0.0** (alignement de marque avec le SDK officiel 2.0).
   On saute volontairement la série `1.x` ; la piste Future post-GA est en `2.x`.
5. **README** : mis à jour **en même temps** que la ROADMAP (table comparative + roadmap +
   différenciateur de compatibilité).

### Faisabilité des features « Advanced » sous SDK 2.0 (vérifiée)

Le SDK officiel 2.0 expose `query(sql, vars)` qui exécute **n'importe quel SurrealQL**, DDL
inclus. Les features avancées en découlent directement :

| Feature              | Réalisation via `query()`                                                           |
| -------------------- | ----------------------------------------------------------------------------------- |
| Vector Search        | `DEFINE INDEX … HNSW/MTREE` + opérateur KNN dans `SELECT`                           |
| Full-Text Search     | `DEFINE ANALYZER` + index `SEARCH` + `@@` + `search::score()`/`search::highlight()` |
| Hybrid Search        | combinaison vecteur + FTS (RRF), en SurrealQL ou côté client                        |
| Schema introspection | `INFO FOR DB` / `INFO FOR TABLE`                                                    |
| Migrations           | orchestration de `DEFINE`/`REMOVE` + table de suivi des migrations                  |
| `DEFINE EVENT`       | triggers serveur déclarés via DDL                                                   |
| Materialized views   | `DEFINE TABLE … AS SELECT …` (modèles read-only)                                    |

**Nuance** : ces capacités dépendent de la **version du serveur**, pas du SDK. Toutes existent
**à la fois en SurrealDB 2.6.x et 3.1** → compatibles avec la promesse lite. Les capacités
d'index exactes seront validées empiriquement sur les **deux** versions serveur (matrice CI).

### Note de versioning (à expliciter dans la doc publique)

La version **du package** passant à `2.0.0` est **indépendante** de la version **du SDK**.
La contrainte de dépendance reste `surrealdb[pydantic]>=2.0.0,<3.0.0`. Le choix de nommer la
GA `2.0.0` est un alignement de marque, pas un couplage de numérotation.

## Différenciateur de compatibilité (lite vs full)

|                                       | Versions SurrealDB supportées                 |
| ------------------------------------- | --------------------------------------------- |
| **SurrealDB-ORM-lite** (SDK officiel) | **2.6.x ET 3.1** (rétrocompatibilité serveur) |
| **SurrealDB-ORM** (SDK custom)        | **3.x uniquement**                            |

C'est un **avantage net** de la lite à mettre en avant : elle reste utilisable sur les
déploiements SurrealDB 2.6.x existants, là où le full ORM exige une migration vers 3.x.

---

## Stratégie de versioning

```text
0.7.0 (actuel)
  │
  ├── Palier 1 — Cœur strict ........... v0.8.0  → v0.22.0  (15 mineures)
  ├── Palier 2 — Extended SDK-2.0 ...... v0.23.0 → v0.29.0  (7 mineures)
  ├── Palier 3 — Advanced .............. v0.30.0 → v0.39.0  (10 mineures)
  ├── Beta (gel d'API) ................. v0.40.0
  │       Release candidates ........... v2.0.0-rcN (tags, pas de ligne roadmap)
  └── Production / GA .................. v2.0.0
          Future post-GA .............. v2.1.0+  (connection pool client-side, tentatif)
```

Les **gros thèmes débordent presque toujours** sur une mineure de suivi une fois confrontés
à l'usage réel : c'est le rôle des **patch releases** (`0.x.1`, `0.x.2`) d'absorber ce
_spillover_ sans décaler la Beta.

---

## Palier 1 — Cœur « strict » (v0.8.0 → v0.22.0)

> Uniquement des primitives SDK 2.0 + philosophie lite. Couverture ≥ 70 % maintenue à chaque
> version ; lint (`ruff`) + `mypy` verts ; suite E2E verte sur **v2.6.0 ET v3.1.3**.

### 🔵 Phase A — Write-path & atomicité

#### v0.8.0 — Transactions ORM (cœur)

**Objectif** : transactions atomiques au niveau CRUD modèle via un context manager.

```python
from surreal_orm_lite import SurrealDBConnectionManager

async with SurrealDBConnectionManager.transaction() as tx:
    user = User(name="Alice", email="alice@example.com")
    await user.save(tx=tx)
    order = Order(user_id=user.id, total=100)
    await order.save(tx=tx)
    # COMMIT automatique en sortie ; CANCEL automatique sur exception
```

- **Primitive SDK** : `BEGIN TRANSACTION` / `COMMIT TRANSACTION` / `CANCEL TRANSACTION`
  via `query()` (compatible 2.6.x et 3.1).
- **Fichiers** : `transaction.py` (nouveau), `model_base.py` (`tx=` sur save/update/merge/delete/refresh).
- **Critères** : commit sur succès, rollback sur exception, propagation des erreurs SDK,
  tests d'atomicité (deux écritures dont une échoue → aucune persistée).

#### v0.9.0 — Transactions ORM (QuerySet & savepoints)

**Objectif** : étendre `tx=` au QuerySet et aux opérations bulk ; savepoints imbriqués.

```python
async with SurrealDBConnectionManager.transaction() as tx:
    users = await User.objects(tx=tx).filter(status="active").exec()
    await User.objects(tx=tx).filter(role="guest").bulk_update(role="member")
```

- **Fichiers** : `query_set.py` (`tx=` sur exec/get/first/all/bulk\_\*), `model_base.py` (`objects(tx=)`).
- **Critères** : lectures et bulk ops participent à la transaction ; savepoints testés
  (rollback partiel si le serveur le supporte, sinon documenté comme non disponible).

#### v0.10.0 — `upsert()` / `update_or_create()` / `get_or_create()`

**Objectif** : insertion-ou-mise-à-jour idempotente.

```python
user, created = await User.objects().update_or_create(
    email="alice@example.com",                 # critères de recherche
    defaults={"name": "Alice", "status": "active"},
)
await user.upsert()                             # insert si absent, update sinon
```

- **Primitive SDK** : `upsert()`.
- **Fichiers** : `model_base.py` (`upsert()`), `query_set.py` (`update_or_create`, `get_or_create`).
- **Critères** : `created` correct ; pas de doublon en cas de course (idempotence) ;
  comportement défini si plusieurs records matchent (lève une erreur explicite).

#### v0.11.0 — `patch()` & opérations atomiques champ/array

**Objectif** : modifications granulaires sans relire/réécrire le record entier.

```python
await user.patch([
    {"op": "replace", "path": "/age", "value": 26},
    {"op": "add", "path": "/tags/-", "value": "premium"},
    {"op": "remove", "path": "/settings/notifications"},
])
# Helpers ergonomiques au-dessus de patch :
await post.atomic_append("tags", "python")
await post.atomic_remove("tags", "spam")
await counter.atomic_increment("views", 1)
```

- **Primitive SDK** : `patch()` (JSON Patch RFC 6902).
- **Fichiers** : `model_base.py` (`patch`, `atomic_append/remove/set_add/increment`),
  `utils.py` (validation des chemins JSON Pointer).
- **Critères** : opérations append/remove/replace/increment testées ; patch sur record
  unique et patch « toute la table » ; validation des chemins.

#### v0.12.0 — `retry_on_conflict` & concurrence optimiste

**Objectif** : réexécuter automatiquement une transaction sur conflit de version.

```python
from surreal_orm_lite import retry_on_conflict

@retry_on_conflict(max_attempts=3, backoff=0.05)
async def transfer(a, b, amount):
    async with SurrealDBConnectionManager.transaction() as tx:
        ...
```

- **Fichiers** : `concurrency.py` (nouveau, décorateur + détection de conflit).
- **Critères** : retente uniquement sur erreur de conflit (pas sur les autres) ;
  respecte `max_attempts` ; backoff testé ; ré-élève l'erreur après épuisement.

### 🟢 Phase B — Calcul côté serveur

#### v0.13.0 — `SurrealFunc` & `server_values`

```python
from surreal_orm_lite import SurrealFunc

await player.save(server_values={"joined_at": SurrealFunc("time::now()")})
await user.save(
    server_values={"password_hash": SurrealFunc("crypto::argon2::generate($password)")},
    extra_vars={"password": raw_password},
)
```

- **Fichiers** : `functions.py` (nouveau, `SurrealFunc`), `model_base.py` (`server_values=`, `extra_vars=`).
- **Critères** : la valeur est évaluée **côté serveur** (pas interpolée en string) ;
  `extra_vars` paramétrés (anti-injection) ; fonctionne sur save() et merge().

#### v0.14.0 — Computed Fields

```python
from surreal_orm_lite import Computed

class User(BaseSurrealModel):
    first_name: str
    last_name: str
    full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")
```

- **Primitive** : `DEFINE FIELD … VALUE <expr>` via `query()` (champ en lecture seule).
- **Fichiers** : `functions.py` (`Computed`), `model_base.py` (exclusion du save, hydratation en lecture).
- **Critères** : champ exclu des INSERT/UPDATE ; valeur lue depuis le serveur ;
  helper de génération du `DEFINE FIELD` documenté.

#### v0.15.0 — `call_function()`

```python
result = await SurrealDBConnectionManager.call_function(
    "fn::acquire_lock", params={"table_id": tid, "pod_id": pid},
)
```

- **Primitive** : RPC `run()` / `RETURN fn::name($args)` via `query()`.
- **Fichiers** : `connection_manager.py` (`call_function`).
- **Critères** : args paramétrés ; valeur de retour désérialisée ; erreurs SDK encapsulées.

### 🟣 Phase C — Auth & DX

#### v0.16.0 — Auth niveau connexion

```python
token = await SurrealDBConnectionManager.signin(
    {"namespace": "ns", "database": "db", "access": "user",
     "variables": {"email": e, "password": p}})
await SurrealDBConnectionManager.authenticate(token)
info = await SurrealDBConnectionManager.info()
await SurrealDBConnectionManager.invalidate()
```

- **Primitive SDK** : `signin` / `signup` / `authenticate` / `invalidate` / `info`.
- **Fichiers** : `connection_manager.py` (méthodes auth + stockage du token de session).
- **Critères** : token conservé/restauré ; `info()` renvoie l'utilisateur courant ;
  `invalidate()` force la ré-auth ; testé root **et** record-user.

#### v0.17.0 — `AuthenticatedUserMixin` (niveau modèle)

```python
class User(BaseSurrealModel, AuthenticatedUserMixin):
    email: str
    password: str

token = await User.signup(email="a@b.c", password="…", access="user")
me = await User.signin(email="a@b.c", password="…")   # → instance User
```

- **Fichiers** : `auth.py` (nouveau, `AuthenticatedUserMixin`).
- **Critères** : signup/signin renvoient une instance de modèle hydratée ; sessions scoped ;
  intégration avec l'auth connexion (v0.16.0).

#### v0.18.0 — Field Aliases & DX

```python
from pydantic import Field

class User(BaseSurrealModel):
    password: str = Field(alias="password_hash")
    model_config = SurrealConfigDict(server_fields=["created_at", "updated_at"])

await user.merge(last_seen=SurrealFunc("time::now()"), refresh=False)  # fire-and-forget
```

- **Fichiers** : `model_base.py` (alias de champ, `server_fields`, `merge(refresh=False)`).
- **Critères** : alias respecté en lecture/écriture ; `server_fields` exclus du save ;
  `refresh=False` saute le SELECT de rafraîchissement.

### 🟡 Phase D — Temps réel

#### v0.19.0 — Live Queries (base)

```python
uuid = await User.objects().live()                   # → query UUID
async for notif in SurrealDBConnectionManager.subscribe_live(uuid):
    print(notif["action"], notif["result"])          # CREATE / UPDATE / DELETE
await SurrealDBConnectionManager.kill(uuid)
```

- **Primitive SDK** : `live()` / `subscribe_live()` (générateur async) / `kill()`.
- **Fichiers** : `live.py` (nouveau), `connection_manager.py` (`subscribe_live`, `kill`).
- **Critères** : nécessite une connexion WebSocket (garde explicite sinon) ;
  notifications reçues pour create/update/delete ; `kill()` arrête le flux.

#### v0.20.0 — `LiveQuerySet` typé

```python
async for event in User.objects().filter(status="active").live_typed():
    user: User = event.record        # désérialisé en instance de modèle
    if event.action == "DELETE": ...
```

- **Primitive SDK** : `live(diff=…)` + `subscribe_live`.
- **Fichiers** : `live.py` (`LiveQuerySet`, dataclass `LiveEvent`).
- **Critères** : notifications désérialisées en modèles ; filtres appliqués ;
  mode `diff=True` (JSON Patch) supporté et documenté.

#### v0.21.0 — Change Feeds / Auto-Resubscribe

```python
async for event in User.objects().live_typed(auto_resubscribe=True):
    ...   # survit aux déconnexions WebSocket transparentes
```

- **Primitive** : live queries + logique de reconnexion + suivi de curseur.
- **Fichiers** : `live.py` (resubscribe), `connection_manager.py` (reconnexion WS).
- **Critères** : reconnexion automatique après coupure ; resubscribe transparent ;
  pas de doublon ni de perte d'événement au-delà du curseur (best-effort documenté).

### 🟠 Phase E — Graphe

#### v0.22.0 — Relations typées natives

```python
edge = await user.relate_typed("purchased", product,
                               data={"quantity": 2}, edge_model=Purchase)
# Utilise insert_relation() plutôt qu'une string RELATE
```

- **Primitive SDK** : `insert_relation()` + tables `TYPE RELATION`.
- **Fichiers** : `model_base.py` (`relate_typed`, désérialisation d'edge en modèle),
  `utils.py` (validation).
- **Critères** : edge créé via `insert_relation` ; données d'edge typées/validées ;
  rétrocompatible avec `relate()` existant (string RELATE).

---

## Palier 2 — Extended SDK-2.0-natif (v0.23.0 → v0.29.0)

> Toujours réalisable avec le SDK officiel, mais features plus « riches ». Sert aussi de
> zone de _spillover_ pour les gros thèmes du palier 1.

| Version     | Thème                                                                                | Réalisation SDK 2.0                                               |
| ----------- | ------------------------------------------------------------------------------------ | ----------------------------------------------------------------- |
| **v0.23.0** | Types riches natifs : `Datetime`, `Duration`, `Decimal`, `Range`, `Uuid`, `Bytes`    | mapping typé Pydantic ↔ types natifs du SDK                       |
| **v0.24.0** | Champs géospatiaux : `Geometry` (Point/Line/Polygon/Multi\*) + `nearby()` / distance | types `Geometry` natifs + `geo::*` + `DEFINE INDEX` via `query()` |
| **v0.25.0** | Moteur embarqué pour tests : `mem://` / `surrealkv://` + fixtures de base            | connexion embarquée du SDK¹                                       |
| **v0.26.0** | Stockage versionné / time-travel : `surrealkv+versioned://`, requêtes `VERSION`      | moteur embarqué versionné¹                                        |
| **v0.27.0** | Subqueries (QuerySets imbriqués dans `filter`/`in`)                                  | sous-`SELECT` compilé via `query()`                               |
| **v0.28.0** | Query cache (TTL + invalidation) côté client                                         | cache client, indépendant du SDK                                  |
| **v0.29.0** | Multi-database : registry de connexions nommées                                      | N × instances `AsyncSurreal`                                      |

¹ **À confirmer** selon la build du SDK (présence de l'extra/feature « embedded »). Si non
disponible dans la distribution PyPI ciblée, v0.25.0/v0.26.0 sont reclassées en Future.

**Critères transverses palier 2** : mêmes garanties que le palier 1 (couverture, lint, mypy,
E2E 2.6.x + 3.1) ; chaque type riche conserve un aller-retour fidèle (sérialisation/désérialisation).

---

## Palier 3 — Advanced (recherche, schéma & DDL, migrations, CLI) — v0.30.0 → v0.39.0

> Features réalisables via `query()` (DDL + opérateurs SurrealQL), engagées au plan (cf.
> décision du 2026-06-06). Mêmes garanties transverses que les paliers 1-2 (couverture, lint,
> mypy, E2E sur 2.6.x + 3.1). Disponibilité d'index validée empiriquement sur les deux serveurs.

### 🔴 Phase F — Schéma & DDL

#### v0.30.0 — Schema introspection

**Objectif** : lire le schéma serveur en objets Python (inspectdb read-only).

- **Primitive** : `INFO FOR DB` / `INFO FOR TABLE x` via `query()`.
- **Fichiers** : `introspection.py` (nouveau).
- **Critères** : tables, champs, index, events listés ; mapping vers une structure typée ;
  testé 2.6.x + 3.1.

#### v0.31.0 — `DEFINE EVENT` (triggers serveur)

- **Primitive** : `DEFINE EVENT … ON TABLE … WHEN … THEN …` via `query()`.
- **Fichiers** : `schema.py` (helpers DDL events).
- **Critères** : déclaration/suppression d'event ; déclenchement vérifié par un test E2E.

#### v0.32.0 — Materialized views

**Objectif** : modèles read-only adossés à une vue serveur.

- **Primitive** : `DEFINE TABLE … AS SELECT …` via `query()`.
- **Fichiers** : `model_base.py` (modèle read-only / `view=`), `schema.py`.
- **Critères** : la vue se met à jour côté serveur ; les écritures sur le modèle sont refusées.

#### v0.33.0 — `TYPE RELATION` enforcement

**Objectif** : contraintes de graphe déclarées (tables d'arêtes typées côté serveur).

- **Primitive** : `DEFINE TABLE … TYPE RELATION IN … OUT …` via `query()`.
- **Fichiers** : `schema.py`, intégration avec `relate_typed` (v0.22.0).
- **Critères** : contraintes in/out posées ; violation rejetée par le serveur (testé).

### 🔎 Phase G — Recherche avancée

#### v0.34.0 — Full-Text Search

```python
posts = await Post.objects().search("title", "surreal database", highlight=True).exec()
```

- **Primitive** : `DEFINE ANALYZER` + `DEFINE INDEX … SEARCH ANALYZER … BM25 HIGHLIGHTS`
  - opérateur `@@` + `search::score()` / `search::highlight()` via `query()`.
- **Fichiers** : `search.py` (nouveau, `SearchField`, helpers analyzer/index), `query_set.py` (`search()`).
- **Critères** : index SEARCH créé ; score BM25 et highlights récupérés ; testé 2.6.x + 3.1.

#### v0.35.0 — Vector Search

```python
neighbors = await Doc.objects().similar_to("embedding", vector, k=10).exec()
```

- **Primitive** : `DEFINE INDEX … HNSW` / `MTREE` + opérateur KNN dans `SELECT` via `query()`.
- **Fichiers** : `search.py` (`VectorField`, `similar_to()`).
- **Critères** : index vectoriel créé ; k plus proches voisins retournés/triés ; distance exposée.

#### v0.36.0 — Hybrid Search (RRF)

- **Primitive** : combinaison FTS + vector, fusion par Reciprocal Rank Fusion.
- **Fichiers** : `search.py` (`hybrid_search()`).
- **Critères** : résultats fusionnés/repondérés ; cohérence vs FTS et vector seuls.

### 🛠️ Phase H — Outillage

#### v0.37.0 — Système de migrations

```bash
surreal-orm-lite makemigrations
surreal-orm-lite migrate
```

- **Primitive** : orchestration de `DEFINE`/`REMOVE` + table de suivi `_migrations` via `query()`.
- **Fichiers** : `migrations/` (nouveau package : autodetect, writer, runner).
- **Critères** : génération depuis les modèles ; application/rollback idempotents ; suivi des
  migrations appliquées ; testé 2.6.x + 3.1.

#### v0.38.0 — CLI `surreal-orm-lite`

- **Objectif** : shell, `migrate`, `makemigrations`, `inspectdb`.
- **Fichiers** : `cli.py` (nouveau, entry point `project.scripts`).
- **Critères** : commandes fonctionnelles ; codes de sortie ; aide ; tests CLI.

#### v0.39.0 — Test fixtures & factories

- **Objectif** : `ModelFactory`, fixtures pytest, données de test reproductibles.
- **Fichiers** : `testing.py` (nouveau).
- **Critères** : génération d'instances valides ; intégration pytest ; doc.

---

## Palier 4 — Stabilisation

### v0.40.0 — Beta Phase (gel d'API)

**Objectif** : feature-complete, durcissement, **gel de l'API publique**.

- Retry/reconnect au **niveau connexion** (résilience réseau, distinct du `retry_on_conflict`).
- Logging configurable (logger nommé, niveaux, requêtes optionnellement tracées).
- **QueryLogger / profiling** (traçage et mesure des requêtes ORM).
- Métriques (compteurs requêtes/erreurs/latence, hooks d'export).
- Benchmarks de performance documentés.
- Documentation complète (docstrings, guide de migration, exemples).
- **Gel d'API** : plus de rupture publique jusqu'à la GA.

**Critères** : suite E2E verte 2.6.x + 3.1, couverture ≥ 75 %, doc API complète,
changelog de gel d'API publié, aucune régression depuis v0.39.0.

### v2.0.0 — Production / GA

- Stabilité production, couverture ≥ **80 %**.
- Documentation complète + guide de migration `0.x → 2.0`.
- Benchmarks de performance publiés.
- Aucune rupture depuis v0.40.0 (l'API gelée en Beta est honorée).
- Release candidates `v2.0.0-rcN` taggées avant la GA (hors lignes de roadmap).

---

## 🔮 Future post-GA (v2.1.0+)

Le périmètre SDK-2.0-faisable est désormais **entièrement engagé** dans les paliers 1-3. Il ne
reste qu'un candidat **tentatif** en post-GA :

- **Connection pool client-side** — le SDK officiel reste mono-connexion ; un pool d'instances
  `AsyncSurreal` côté client est envisageable mais non prioritaire (à évaluer après la GA).

---

## 🚫 Hors périmètre lite (full-ORM only, même avec SDK 2.0)

| Feature                    | Raison                                             |
| -------------------------- | -------------------------------------------------- |
| Custom SDK (`surreal_sdk`) | Choix architectural fondateur du full ORM.         |
| Manipulation CBOR custom   | Gérée en interne par le SDK officiel, non exposée. |

---

## Mises à jour README requises (livrées avec ce design)

1. **Section `## Roadmap`** : refléter la nouvelle progression (cœur → extended → advanced →
   Beta v0.40.0 → GA v2.0.0 → Future v2.x).
2. **Table `## SurrealDB-ORM-lite vs SurrealDB-ORM`** : faire passer de `❌` à « planned vX »
   **toutes** les features désormais engagées au plan grâce au SDK 2.0 (Transactions, JWT Auth,
   Atomic ops, Live Models/CDC, Computed/SurrealFunc, relations typées, vector/FTS/hybrid,
   introspection, migrations, CLI, events, vues…). Garder `❌`/full-ORM-only uniquement pour :
   Custom SDK et CBOR custom.
3. **Différenciateur de compatibilité** : nouvelle ligne/encart — lite = **2.6.x + 3.1**,
   full = **3.x uniquement**.

---

## Critères de succès du design (cette spec)

1. Chaque version des paliers 1, 2 et 3 a un **objectif**, une **primitive / réalisation SDK 2.0**
   identifiée, les **fichiers** impactés et des **critères d'acceptation**.
2. Beta = v0.40.0, GA = v2.0.0, Future post-GA = v2.x — cohérent dans toute la doc.
3. Aucune feature du plan ne dépend d'un SDK custom ; tout passe par le SDK officiel (API native
   ou `query()` SurrealQL).
4. ROADMAP.md et README.md alignés sur cette spec.
5. Le différenciateur de compatibilité (2.6.x + 3.1 vs 3.x) apparaît dans README **et** ROADMAP.
