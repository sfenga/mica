from __future__ import annotations

from importlib.resources import files

from .connection import PostgresDatabase


def migrate(database: PostgresDatabase) -> None:
    migration_dir = files("mica.infrastructure.database").joinpath("sql")
    migrations = sorted(path for path in migration_dir.iterdir() if path.name.endswith(".up.sql"))
    with database.transaction():
        with database.connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext('mica-schema-migrations'))")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migration "
                "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            applied = {
                row["version"] for row in connection.execute("SELECT version FROM schema_migration")
            }
            for migration in migrations:
                version = migration.name.removesuffix(".up.sql")
                if version in applied:
                    continue
                connection.execute(migration.read_text(encoding="utf-8"))
                connection.execute("INSERT INTO schema_migration(version) VALUES (%s)", (version,))
