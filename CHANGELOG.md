# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
