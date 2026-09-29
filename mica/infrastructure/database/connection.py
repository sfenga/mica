from __future__ import annotations

from contextlib import contextmanager
from threading import local
from typing import Iterator


class PostgresDatabase:
    """Small transaction manager shared by all PostgreSQL repositories."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._state = local()

    @staticmethod
    def _driver():
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ModuleNotFoundError as exc:
            raise RuntimeError("PostgreSQL support requires `pip install psycopg[binary]`") from exc
        return psycopg, dict_row

    @contextmanager
    def transaction(self) -> Iterator[None]:
        active = getattr(self._state, "connection", None)
        if active is not None:
            yield
            return
        psycopg, dict_row = self._driver()
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            self._state.connection = connection
            try:
                yield
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                self._state.connection = None

    @contextmanager
    def connection(self):
        active = getattr(self._state, "connection", None)
        if active is not None:
            yield active
            return
        with self.transaction():
            yield self._state.connection

    def ping(self) -> None:
        with self.connection() as connection:
            connection.execute("SELECT 1")
