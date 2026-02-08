# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-02-07

### Added

- **Model Signals**: Django-style event system for model lifecycle
  - `Signal` class for pre/post event handlers
  - `AroundSignal` class for context manager-style wrapping signals with `yield`
  - `pre_save` / `post_save` - Fired before/after `save()` operations
  - `pre_update` / `post_update` - Fired before/after `update()` and `merge()` operations
  - `pre_delete` / `post_delete` - Fired before/after `delete()` operations
  - `around_save` / `around_update` / `around_delete` - Wrap operations for timing, logging, etc.
  - `connect(model_class)` decorator for registering handlers
  - `disconnect(handler, model_class)` for removing handlers
  - `clear()` for removing all handlers
  - `has_handlers()` for checking if handlers are registered

- `post_save` signal includes `created` flag to distinguish new records
- `pre_update` / `post_update` signals include `update_fields` list
- New test file `tests/test_signals.py` with unit and e2e tests

### Changed

- `save()`, `update()`, `merge()`, `delete()` now emit signals when handlers are registered
- Internal `_do_save()` method extracted from `save()` for signal integration

## [0.3.0] - 2026-02-05

### Added

- **Aggregation Functions**: New aggregation classes for database calculations
  - `Count()` - Count records
  - `Sum(field)` - Sum numeric field values
  - `Avg(field)` - Calculate average of numeric field
  - `Min(field)` - Find minimum value
  - `Max(field)` - Find maximum value

- **QuerySet Aggregation Methods**: Shortcut methods for common aggregations
  - `count()` - Returns count as integer directly
  - `sum(field)` - Returns sum as float/int
  - `avg(field)` - Returns average as float
  - `min(field)` - Returns minimum value
  - `max(field)` - Returns maximum value

- **GROUP BY Support**: Django-style grouping with annotations
  - `values(*fields)` - Specify fields for GROUP BY
  - `annotate(**aggregations)` - Add aggregation annotations

- **exists() Method**: Efficiently check if records exist

- **raw_query() Class Method**: Execute arbitrary SurrealQL queries with variables

- New test file `tests/test_aggregations.py` with comprehensive unit and e2e tests

### Changed

- QuerySet now tracks `_group_by_fields` and `_annotations` for GROUP BY queries
- `exec()` method now handles GROUP BY queries differently, returning dicts instead of model instances

## [0.2.2] - 2026-02-05

### Fixed

- Corrected project URLs in package metadata (case sensitivity)

## [0.2.1] - 2026-02-04

### Fixed

- Fixed `refresh()` method not updating the model instance
- Fixed `refresh()` not handling SDK 1.0.8 list responses
- Fixed `save()` not updating instance with auto-generated ID
- Fixed SQL query order: `ORDER BY` now comes before `LIMIT/START` (SurrealDB requirement)
- Fixed `set_data` validator not always returning data
- Fixed typo in error message: `primirary_key` → `primary_key`
- Removed ineffective `del self` in `delete()` method
- Fixed dead test `failed_model_validation` that was never executed

### Changed

- `SurrealDbNotFoundError` now inherits from `SurrealDbError` for backward compatibility
- Test database connection now configurable via `SURREALDB_HOST` and `SURREALDB_PORT` environment variables
- Improved docstrings for `unset_connection()` and `is_password_set()`
- Translated French comments to English in `connection_manager.py`
- Renamed variable `test` → `result` in `update()` method

### Added

- Added `codecov.yml` configuration (disabled patch coverage check)
- Added test for `save()` with auto-generated ID
- Added test for `contains` lookup operator

## [0.2.0] - 2026-02-02

### Added

- Initial release of **Surreal ORM Lite**
- Support for official SurrealDB Python SDK 1.0.8
- Compatibility with SurrealDB 2.6.0
- Django-style ORM with `BaseSurrealModel` base class
- `QuerySet` with fluent query builder
- Filter lookups: `exact`, `gt`, `gte`, `lt`, `lte`, `in`, `contains`, `startswith`, etc.
- CRUD operations: `save()`, `update()`, `merge()`, `delete()`, `refresh()`
- `SurrealDBConnectionManager` for connection management
- HTTP and WebSocket connection support
- Custom primary key configuration via `SurrealConfigDict`
- Pydantic 2.x validation support
- Custom exceptions: `SurrealDbError`, `SurrealDbConnectionError`, etc.
- Full async/await support
- 97%+ test coverage

### Changed

- Migrated from custom SDK to official SurrealDB SDK 1.0.8
- Updated API to match SDK 1.0.8 response formats
- `signin()` now uses dictionary format: `{"username": ..., "password": ...}`

### Fixed

- Handle SDK 1.0.8 returning lists for single record `select()`
- Handle SDK 1.0.8 returning error strings instead of exceptions
- Escape special characters in record IDs (e.g., `@` in email addresses)

## [0.1.x] - Previous Versions

Previous versions were part of the SurrealDB-ORM project with a custom SDK implementation. This project (Surreal ORM Lite) is a fork focused on using the official SDK only.
