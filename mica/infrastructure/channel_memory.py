from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from threading import RLock
from typing import Dict, Optional, Tuple

from mica.domain import (
    ExternalEvent,
    ExternalIdentity,
    IssueOrigin,
    OutboxMessage,
    PlatformUser,
    utc_now,
)


class InMemoryChannelRepository:
    def __init__(self) -> None:
        self._users: Dict[str, PlatformUser] = {}
        self._identities: Dict[tuple[str, str, str], ExternalIdentity] = {}
        self._events: Dict[tuple[str, str, str], ExternalEvent] = {}
        self._origins: Dict[str, IssueOrigin] = {}
        self._outbox: Dict[str, OutboxMessage] = {}
        self._outbox_keys: Dict[tuple[str, str, str], str] = {}
        self.lock = RLock()

    def delete_chat_data(self, event_ids: list[str]) -> None:
        with self.lock:
            self._outbox = {k: m for k, m in self._outbox.items()
                            if m.source_event_id not in event_ids}
            self._outbox_keys = {k: v for k, v in self._outbox_keys.items() if v in self._outbox}

    def delete_issue_data(self, issue_id: str, event_ids: list[str]) -> None:
        with self.lock:
            ingress_keys = {
                (event.channel, event.external_event_id)
                for event in self._events.values()
                if event.issue_id == issue_id
            }
            self._outbox = {
                key: message
                for key, message in self._outbox.items()
                if message.source_event_id not in event_ids
                and (message.channel, message.source_event_id) not in ingress_keys
                and message.payload.get("issue_id") != issue_id
            }
            self._outbox_keys = {
                key: value for key, value in self._outbox_keys.items() if value in self._outbox
            }
            self._events = {
                key: event for key, event in self._events.items() if event.issue_id != issue_id
            }
            self._origins.pop(issue_id, None)

    def resolve_identity(
        self,
        channel: str,
        tenant_key: str,
        external_user_id: str,
        workspace_id: str,
        display_name: str,
    ) -> Tuple[PlatformUser, ExternalIdentity]:
        key = (channel, tenant_key, external_user_id)
        with self.lock:
            identity = self._identities.get(key)
            if identity is not None:
                return deepcopy(self._users[identity.user_id]), deepcopy(identity)
            user = PlatformUser(workspace_id=workspace_id, display_name=display_name)
            identity = ExternalIdentity(
                channel=channel,
                tenant_key=tenant_key,
                external_user_id=external_user_id,
                user_id=user.id,
            )
            self._users[user.id] = user
            self._identities[key] = identity
            return deepcopy(user), deepcopy(identity)

    def reserve_event(self, event: ExternalEvent) -> Tuple[ExternalEvent, bool]:
        key = (event.channel, event.tenant_key, event.external_event_id)
        with self.lock:
            existing = self._events.get(key)
            if existing is not None:
                return deepcopy(existing), False
            self._events[key] = deepcopy(event)
            return deepcopy(event), True

    def attach_issue(
        self, channel: str, tenant_key: str, external_event_id: str, issue_id: str
    ) -> None:
        with self.lock:
            event = self._events[(channel, tenant_key, external_event_id)]
            event.issue_id = issue_id

    def add_origin(self, origin: IssueOrigin) -> IssueOrigin:
        with self.lock:
            self._origins.setdefault(origin.issue_id, deepcopy(origin))
            return deepcopy(self._origins[origin.issue_id])

    def get_origin(self, issue_id: str) -> Optional[IssueOrigin]:
        with self.lock:
            origin = self._origins.get(issue_id)
            return deepcopy(origin) if origin else None

    def find_origin(
        self, channel: str, tenant_key: str, external_message_id: str
    ) -> Optional[IssueOrigin]:
        with self.lock:
            origin = next(
                (
                    item
                    for item in self._origins.values()
                    if item.channel == channel
                    and item.tenant_key == tenant_key
                    and item.external_message_id == external_message_id
                ),
                None,
            )
            return deepcopy(origin) if origin else None

    def add_outbox(self, message: OutboxMessage) -> OutboxMessage:
        key = (message.channel, message.source_event_id, message.message_type)
        with self.lock:
            existing_id = self._outbox_keys.get(key)
            if existing_id:
                return deepcopy(self._outbox[existing_id])
            self._outbox[message.id] = deepcopy(message)
            self._outbox_keys[key] = message.id
            return deepcopy(message)

    def claim_outbox(self, worker_id: str, lease_seconds: int = 60) -> Optional[OutboxMessage]:
        now = utc_now()
        with self.lock:
            for item in self._outbox.values():
                if (
                    item.status == "sending"
                    and item.lease_expires_at
                    and item.lease_expires_at <= now
                ):
                    item.status = "pending"
                    item.claimed_by = None
                    item.lease_expires_at = None
            candidates = sorted(
                self._outbox.values(), key=lambda item: (item.available_at, item.created_at)
            )
            for item in candidates:
                if item.status != "pending" or item.available_at > now:
                    continue
                item.status = "sending"
                item.claimed_by = worker_id
                item.attempt += 1
                item.lease_expires_at = now + timedelta(seconds=lease_seconds)
                item.updated_at = now
                return deepcopy(item)
        return None

    def mark_outbox_sent(self, message_id: str, worker_id: str) -> bool:
        with self.lock:
            item = self._outbox.get(message_id)
            if item is None or item.claimed_by != worker_id or item.status != "sending":
                return False
            item.status = "sent"
            item.claimed_by = None
            item.lease_expires_at = None
            item.updated_at = utc_now()
            return True

    def retry_outbox(self, message_id: str, worker_id: str, error: str, delay_seconds: int) -> bool:
        with self.lock:
            item = self._outbox.get(message_id)
            if item is None or item.claimed_by != worker_id or item.status != "sending":
                return False
            item.status = "pending"
            item.claimed_by = None
            item.lease_expires_at = None
            item.last_error = error
            item.available_at = utc_now() + timedelta(seconds=delay_seconds)
            item.updated_at = utc_now()
            return True

    def fail_outbox(self, message_id: str, worker_id: str, error: str) -> bool:
        with self.lock:
            item = self._outbox.get(message_id)
            if item is None or item.claimed_by != worker_id or item.status != "sending":
                return False
            item.status = "failed"
            item.claimed_by = None
            item.lease_expires_at = None
            item.last_error = error
            item.updated_at = utc_now()
            return True

    def list_outbox(self):
        with self.lock:
            return [deepcopy(item) for item in self._outbox.values()]
