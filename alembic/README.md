# Alembic Migration Guide

## Initialize current schema baseline

```bash
alembic revision --autogenerate -m "baseline"
alembic upgrade head
```

## Create new migration

```bash
alembic revision --autogenerate -m "add_xxx"
```

## Upgrade / Downgrade

```bash
alembic upgrade head
alembic downgrade -1
```

Notes:
- The DB URL is read from `alembic.ini` first.
- If not set, Alembic falls back to `src.config.settings.get_database_url()`.
