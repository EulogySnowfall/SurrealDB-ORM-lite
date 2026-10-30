"""``python -m surreal_orm_lite.cli``: the ``surreal-orm-lite`` command line."""

from __future__ import annotations

from .commands import main

if __name__ == "__main__":
    raise SystemExit(main())
