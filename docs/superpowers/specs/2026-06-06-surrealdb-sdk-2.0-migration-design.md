# Migration vers SurrealDB Python SDK 2.0.0 (support SurrealDB 3.1.3)

- **Date** : 2026-06-06
- **Statut** : Approuvé (design)
- **Version cible du projet** : 0.7.0 (changement de comportement de `id`)

## Objectif

Mettre à jour `surreal-orm-lite` pour utiliser le SDK Python officiel `surrealdb` **2.0.0**
(qui ajoute le support du protocole **SurrealDB 3.x**) et fonctionner contre un serveur
**SurrealDB 3.1.3**, tout en gardant **toutes les méthodes de l'ORM fonctionnelles**.

Décisions de cadrage (validées avec l'utilisateur) :

1. **Portée** : refonte vers l'API native Pydantic du SDK 2.0 (extra `pydantic`,
   `RecordID` natif, exceptions structurées), pas un simple bump de dépendance.
2. **CI serveur** : matrice testant **v2.6.0 ET v3.1.3** (rétrocompatibilité serveur).
3. **Champ `id`** : exposer le **`RecordID` natif** (changement de comportement public assumé).

## Contexte technique

- SDK actuel : `surrealdb>=1.0.8` (1.0.8 verrouillé). Dernière version PyPI : `2.0.0`.
- La branche `main` du SDK développe une **3.0.0** avec un *lazy query builder* (rupture) :
  on borne donc la dépendance à `<3.0.0`.
- Changelog SDK **2.0.0 / 2.0.0a1** (faits confirmés) :
  - « SurrealDB 3.x protocol and feature support » (#230).
  - « Structured error hierarchy and `ServerError` with SurrealDB 3.x–style kind/details » (#233)
    → **les erreurs sont levées en exceptions**, plus retournées en strings.
  - Python minimum 3.10 (le projet est déjà `>=3.11`).
  - L'extra `pydantic` existe depuis 1.0.8 : il assure la validation/sérialisation propre
    des champs `RecordID` dans les `BaseModel`.

### Surface d'utilisation du SDK dans le code

Imports : `AsyncSurreal`, `RecordID`.
Méthodes client : `connect`, `signin`, `use`, `query`, `select`, `create`, `update`,
`merge`, `delete`, `insert`, `close`.

Le code actuel contient de nombreux contournements explicites du SDK 1.0.8 :
- « SDK 1.0.8 returns a list even for single record select »
- « SDK 1.0.8 returns error message as string instead of raising exception »
- « SDK 1.0.8 returns list directly from query() »

Ces contournements sont la cible principale de la migration.

## Architecture & composants

### 1. Dépendance (`pyproject.toml`, `uv.lock`)

```toml
dependencies = [
    "pydantic>=2.12.5",
    "surrealdb[pydantic]>=2.0.0,<3.0.0",
]
```

- `uv.lock` régénéré et committé.
- `version = "0.7.0"`.

### 2. Démarche empirique d'abord (TDD)

Avant toute correction, **mesurer** le comportement réel du SDK 2.0 contre SurrealDB 3.1.3 :

1. Lancer `docker run -d --name surreal -p 8000:8000 surrealdb/surrealdb:v3.1.3 start --user root --pass root`.
2. Installer `surrealdb[pydantic]==2.0.0`, exécuter la suite existante.
3. Cataloguer chaque rupture observée :
   - forme de retour exacte de `query()` (list directe vs `[{result: ...}]`),
   - forme de retour de `select()` pour un enregistrement unique (dict vs list),
   - classe d'exception exacte levée par le SDK (pour `ServerError` & co),
   - comportement de `RecordID` (attributs `.id`, `.table_name`, parsing).
4. Chaque rupture rouge pilote un correctif (un test → un fix).

### 3. Gestion des erreurs structurées

- Isoler l'import de la/les classe(s) d'exception du SDK dans **un seul module**
  (p.ex. `exceptions.py` ou `connection_manager.py`) pour éviter de disperser
  les `import surrealdb` internes.
- [`model_base.py`](../../../src/surreal_orm_lite/model_base.py) `_do_save` :
  remplacer les branches `isinstance(result, str) and "already exists"` par un
  `try/except` capturant l'exception SDK et la ré-encapsulant en `SurrealDbError`
  (message « already exists » préservé). Idem pour `update`.
- La classe d'exception exacte est confirmée à l'étape 2.

### 4. Refonte `RecordID` natif (cœur)

- **`set_data`** (validateur `mode="before"`) : ne plus faire
  `str(data["id"]).split(":")[1]`. Conserver le `RecordID` tel quel ;
  le champ `id` est désormais typé `RecordID`.
- **`get_id()`** : retourne le `RecordID` (ou `None`).
  Ajouter un helper `get_raw_id()` retournant la partie identifiant (`RecordID.id`)
  quand un code interne a besoin de la valeur brute.
- **`_get_thing()`**, `save`, `update`, `merge`, `delete`, `refresh` :
  passer le `RecordID` **directement** aux méthodes du SDK
  (`client.select(record_id)`, `client.update(record_id, …)`, etc.)
  plutôt que de fabriquer `f"{table}:{id}"`.
  La construction de string reste réservée aux cas sans `RecordID`, avec une
  garde empêchant le double-préfixe (`Table:Table:id`).
- **`from_db`** : principe inchangé — Pydantic valide le `RecordID` via l'extra.
- **Sérialisation** : `model_dump(exclude={"id"})` conservé pour create/update ;
  s'appuyer sur l'extra `pydantic` pour sérialiser proprement les `RecordID` imbriqués.

### 5. Formes de retour `query()` / `select()`

- Conserver la gestion **défensive** existante (list vs dict, wrappé vs non-wrappé)
  dans [`query_set.py`](../../../src/surreal_orm_lite/query_set.py) et `model_base.py`.
- **Simplifier uniquement** les branches que l'étape 2 prouve déterministes en 2.0,
  sans casser les cas limites.
- Mettre à jour les commentaires « SDK 1.0.8 … » pour refléter le comportement 2.0.

### 6. Tests & CI

- [`.github/workflows/ci.yml`](../../../.github/workflows/ci.yml) : ajouter une matrice
  de version serveur **`v2.6.0` et `v3.1.3`** (combinée à la matrice Python 3.11–3.14).
- Tests unitaires comparant `id` à une string (`test_unit.py`, et tous les fichiers
  important `RecordID`) : **mis à jour** pour la sémantique `RecordID`.
- Seuil de couverture **70 %** maintenu (`--cov-fail-under=70`).
- Suite **verte localement sur les deux versions serveur** avant tout push.

### 7. Versioning & documentation

- Bump **0.7.0**.
- Entrée **CHANGELOG** : dépendance SDK 2.0, support SurrealDB 3.x,
  **rupture** `id` → `RecordID`, erreurs en exceptions structurées.
- README / docs : exemples mis à jour pour la sémantique `RecordID`.

## Hors périmètre (YAGNI)

- Le *lazy query builder* du SDK 3.0 (borné par `<3.0.0`).
- La méthode `.into(dataclass)` du SDK 3.x.
- Toute refonte de l'API publique au-delà du changement de type de `id`.

## Risques & atténuations

| Risque | Atténuation |
| --- | --- |
| Formes de retour 2.0 mal anticipées | Étape empirique (§2) avant correction ; gestion défensive conservée. |
| Rupture `id` chez les utilisateurs | Documentée au CHANGELOG ; bump mineur 0.7.0 ; README mis à jour. |
| Classe d'exception SDK inconnue | Confirmée empiriquement (§2) ; import isolé dans un module. |
| Régression rétrocompat serveur 2.6.0 | Matrice CI 2.6.0 + 3.1.3. |

## Critères de succès

1. `surrealdb[pydantic]>=2.0.0,<3.0.0` installé, `uv.lock` à jour.
2. Toute la suite passe localement contre **v2.6.0 et v3.1.3**.
3. Lint (`ruff check` + `ruff format --check`) et `mypy` verts sur `src/`.
4. Couverture ≥ 70 %.
5. Les méthodes publiques de l'ORM (save/update/merge/delete/refresh, QuerySet,
   relations, agrégations, signaux) fonctionnent à l'identique, `id` excepté.
6. CHANGELOG et version (0.7.0) à jour.
