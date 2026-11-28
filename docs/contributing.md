# Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m "Add amazing feature"`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## Documentation

The guides under `docs/` are Markdown files (`docs/*.md`, the index `docs/README.md`): edit those. The
[documentation site](https://docs.page/EulogySnowfall/SurrealDB-ORM-lite) renders the `.mdx` page generated next to
each guide (`docs/index.mdx` from the index); never edit a `.mdx` file by hand. After changing a guide, regenerate
the pages and commit them with it:

```bash
uv run python scripts/sync_docs_site.py --fix
```

The test suite fails while a page differs from its guide (`tests/test_docs_site.py`).
