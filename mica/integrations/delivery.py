from __future__ import annotations

import logging
from threading import Event, Thread
from typing import Protocol
from uuid import uuid4

from mica.application.channels import TaskNotificationProjector
from mica.application.ports import ChannelRepository
from mica.domain import OutboxMessage


class MessageSender(Protocol):
    def send(self, message: OutboxMessage) -> None: ...


class ChannelDeliveryWorker:
    def __init__(
        self,
        channels: ChannelRepository,
        projector: TaskNotificationProjector,
        senders: dict[str, MessageSender],
        poll_interval: float = 2.0,
        max_attempts: int = 8,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.channels = channels
        self.projector = projector
        self.senders = senders
        self.poll_interval = poll_interval
        self.max_attempts = max_attempts
        self.worker_id = f"channel-{uuid4().hex[:10]}"
        self._stop = Event()
        self._wake = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name=self.worker_id, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=max(2.0, self.poll_interval + 1))

    def wake(self) -> None:
        self._wake.set()

    def run_once(self) -> bool:
        self.projector.run_once()
        message = self.channels.claim_outbox(self.worker_id)
        if message is None:
            return False
        sender = self.senders.get(message.channel)
        if sender is None:
            self._retry_or_fail(message, "channel sender is not configured", 60)
            return False
        try:
            sender.send(message)
        except Exception as exc:
            delay = min(300, 2 ** min(message.attempt, 8))
            self._retry_or_fail(message, str(exc), delay)
            return False
        self.channels.mark_outbox_sent(message.id, self.worker_id)
        return True

    def _retry_or_fail(self, message: OutboxMessage, error: str, delay: int) -> None:
        if message.attempt >= self.max_attempts:
            self.channels.fail_outbox(message.id, self.worker_id, error)
        else:
            self.channels.retry_outbox(message.id, self.worker_id, error, delay)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                while self.run_once():
                    continue
            except Exception:
                logging.getLogger(__name__).exception("channel delivery loop failed; retrying")
            self._wake.wait(self.poll_interval)
            self._wake.clear()
