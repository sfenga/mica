from __future__ import annotations

import logging
from datetime import datetime, timezone
from queue import Queue
from threading import Event, RLock, Thread
from typing import Dict, List, Optional, Set, Tuple

from mica.application.ports import EventRepository
from mica.domain import TaskEvent


class EventBus:
    """In-process fan-out; production transports can implement the same publish port."""

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[Queue[TaskEvent]]] = {}
        self._lock = RLock()
        self._published: Set[str] = set()

    def subscribe(self, task_id: str) -> Queue[TaskEvent]:
        queue: Queue[TaskEvent] = Queue()
        with self._lock:
            self._subscribers.setdefault(task_id, []).append(queue)
        return queue

    def unsubscribe(self, task_id: str, queue: Queue[TaskEvent]) -> None:
        with self._lock:
            subscribers = self._subscribers.get(task_id, [])
            if queue in subscribers:
                subscribers.remove(queue)
            if not subscribers:
                self._subscribers.pop(task_id, None)

    def publish(self, event: TaskEvent) -> None:
        with self._lock:
            if event.id in self._published:
                return
            self._published.add(event.id)
            if len(self._published) > 10_000:
                self._published.clear()
                self._published.add(event.id)
            subscribers = list(self._subscribers.get(event.task_id, []))
        for subscriber in subscribers:
            subscriber.put(event)


class DatabaseEventRelay:
    """Relays events written by a separate Daemon process into the API process."""

    def __init__(self, events: EventRepository, bus: EventBus, poll_interval: float = 0.25) -> None:
        self.events = events
        self.bus = bus
        self.poll_interval = poll_interval
        self._cursor: Tuple[datetime, str] = (datetime.min.replace(tzinfo=timezone.utc), "")
        self._stop = Event()
        self._thread: Optional[Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        existing = self.events.list()
        if existing:
            last = max(existing, key=lambda event: (event.created_at, event.id))
            self._cursor = (last.created_at, last.id)
        self._stop.clear()
        self._thread = Thread(target=self._run, name="mica-event-relay", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval):
            try:
                for event in self.events.list_after(*self._cursor):
                    self.bus.publish(event)
                    self._cursor = (event.created_at, event.id)
            except Exception:
                logging.getLogger(__name__).exception("event relay failed; retrying")
