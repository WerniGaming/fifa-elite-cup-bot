"""
Zentrale Datenbank-Anbindung. Bot und Website teilen sich dieselbe Postgres-DB.
"""
from __future__ import annotations
import os
import asyncpg

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=os.environ["DATABASE_URL"],
            min_size=1,
            max_size=5,
        )
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB-Pool nicht initialisiert. init_pool() zuerst aufrufen.")
    return _pool
