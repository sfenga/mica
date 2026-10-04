from contextlib import contextmanager
from copy import deepcopy
from threading import RLock, local


class MemoryTransactionManager:
    """Share a lock and rollback boundary across the in-memory adapters."""

    def __init__(self, repositories):
        self.repositories = repositories
        self.lock = RLock()
        self.state = local()
        for repository in repositories:
            if hasattr(repository, "_lock"):
                repository._lock = self.lock
            if hasattr(repository, "lock"):
                repository.lock = self.lock

    @contextmanager
    def transaction(self):
        with self.lock:
            if getattr(self.state, "active", False):
                yield
                return
            snapshots = [
                {
                    key: deepcopy(value)
                    for key, value in vars(repository).items()
                    if isinstance(value, (dict, list))
                }
                for repository in self.repositories
            ]
            self.state.active = True
            try:
                yield
            except Exception:
                for repository, snapshot in zip(self.repositories, snapshots):
                    for key, value in snapshot.items():
                        setattr(repository, key, value)
                raise
            finally:
                self.state.active = False
