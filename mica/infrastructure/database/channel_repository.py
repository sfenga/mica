from __future__ import annotations

import json
from typing import Optional, Tuple

from mica.domain import (
    ExternalEvent,
    ExternalIdentity,
    IssueOrigin,
    OutboxMessage,
    PlatformUser,
)

from .connection import PostgresDatabase


def _outbox(row) -> Optional[OutboxMessage]:
    if row is None:
        return None
    return OutboxMessage(
        id=row["id"],
        channel=row["channel"],
        destination=row["destination"],
        source_event_id=row["source_event_id"],
        message_type=row["message_type"],
        payload=row["payload"],
        status=row["status"],
        attempt=row["attempt"],
        available_at=row["available_at"],
        claimed_by=row["claimed_by"],
        lease_expires_at=row["lease_expires_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PostgresChannelRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def delete_issue_data(self, issue_id: str, event_ids: list[str]) -> None:
        with self.database.connection() as connection:
            connection.execute(
                """DELETE FROM outbox_message WHERE source_event_id=ANY(%s)
                OR payload->>'issue_id'=%s
                OR (channel, source_event_id) IN (SELECT channel, external_event_id
                    FROM external_event WHERE issue_id=%s)""",
                (event_ids, issue_id, issue_id),
            )
            connection.execute("DELETE FROM external_event WHERE issue_id=%s", (issue_id,))
            connection.execute("DELETE FROM issue_origin WHERE issue_id=%s", (issue_id,))

    def resolve_identity(
        self,
        channel: str,
        tenant_key: str,
        external_user_id: str,
        workspace_id: str,
        display_name: str,
    ) -> Tuple[PlatformUser, ExternalIdentity]:
        lock_key = f"identity:{channel}:{tenant_key}:{external_user_id}"
        with self.database.connection() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))
            row = connection.execute(
                """SELECT u.*, i.channel, i.tenant_key, i.external_user_id, i.created_at AS identity_created_at
                FROM external_identity i JOIN platform_user u ON u.id=i.user_id
                WHERE i.channel=%s AND i.tenant_key=%s AND i.external_user_id=%s""",
                (channel, tenant_key, external_user_id),
            ).fetchone()
            if row is None:
                user = PlatformUser(workspace_id=workspace_id, display_name=display_name)
                identity = ExternalIdentity(
                    channel=channel,
                    tenant_key=tenant_key,
                    external_user_id=external_user_id,
                    user_id=user.id,
                )
                connection.execute(
                    "INSERT INTO platform_user(id, workspace_id, display_name, created_at) VALUES (%s,%s,%s,%s)",
                    (user.id, user.workspace_id, user.display_name, user.created_at),
                )
                connection.execute(
                    """INSERT INTO external_identity(channel, tenant_key, external_user_id, user_id, created_at)
                    VALUES (%s,%s,%s,%s,%s)""",
                    (channel, tenant_key, external_user_id, user.id, identity.created_at),
                )
                return user, identity
        user = PlatformUser(
            id=row["id"],
            workspace_id=row["workspace_id"],
            display_name=row["display_name"],
            created_at=row["created_at"],
        )
        identity = ExternalIdentity(
            channel=row["channel"],
            tenant_key=row["tenant_key"],
            external_user_id=row["external_user_id"],
            user_id=row["id"],
            created_at=row["identity_created_at"],
        )
        return user, identity

    def reserve_event(self, event: ExternalEvent) -> Tuple[ExternalEvent, bool]:
        with self.database.connection() as connection:
            inserted = connection.execute(
                """INSERT INTO external_event(
                    channel, tenant_key, external_event_id, event_type, payload_hash, issue_id, created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (channel, tenant_key, external_event_id) DO NOTHING
                RETURNING *""",
                (
                    event.channel,
                    event.tenant_key,
                    event.external_event_id,
                    event.event_type,
                    event.payload_hash,
                    event.issue_id,
                    event.created_at,
                ),
            ).fetchone()
            row = (
                inserted
                or connection.execute(
                    """SELECT * FROM external_event
                WHERE channel=%s AND tenant_key=%s AND external_event_id=%s FOR UPDATE""",
                    (event.channel, event.tenant_key, event.external_event_id),
                ).fetchone()
            )
        return self._event(row), inserted is not None

    def attach_issue(
        self, channel: str, tenant_key: str, external_event_id: str, issue_id: str
    ) -> None:
        with self.database.connection() as connection:
            connection.execute(
                """UPDATE external_event SET issue_id=%s
                WHERE channel=%s AND tenant_key=%s AND external_event_id=%s""",
                (issue_id, channel, tenant_key, external_event_id),
            )

    def add_origin(self, origin: IssueOrigin) -> IssueOrigin:
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO issue_origin(
                    issue_id, channel, tenant_key, conversation_id, external_message_id,
                    thread_id, created_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (issue_id) DO NOTHING""",
                (
                    origin.issue_id,
                    origin.channel,
                    origin.tenant_key,
                    origin.conversation_id,
                    origin.external_message_id,
                    origin.thread_id,
                    origin.created_at,
                ),
            )
        return origin

    def get_origin(self, issue_id: str) -> Optional[IssueOrigin]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM issue_origin WHERE issue_id=%s", (issue_id,)
            ).fetchone()
        if row is None:
            return None
        return IssueOrigin(**dict(row))

    def find_origin(
        self, channel: str, tenant_key: str, external_message_id: str
    ) -> Optional[IssueOrigin]:
        with self.database.connection() as connection:
            row = connection.execute(
                """SELECT * FROM issue_origin
                WHERE channel=%s AND tenant_key=%s AND external_message_id=%s""",
                (channel, tenant_key, external_message_id),
            ).fetchone()
        return IssueOrigin(**dict(row)) if row else None

    def add_outbox(self, message: OutboxMessage) -> OutboxMessage:
        with self.database.connection() as connection:
            row = connection.execute(
                """INSERT INTO outbox_message(
                    id, channel, destination, source_event_id, message_type, payload, status,
                    attempt, available_at, claimed_by, lease_expires_at, last_error, created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (channel, source_event_id, message_type) DO UPDATE
                    SET source_event_id=EXCLUDED.source_event_id
                RETURNING *""",
                (
                    message.id,
                    message.channel,
                    message.destination,
                    message.source_event_id,
                    message.message_type,
                    json.dumps(message.payload),
                    message.status,
                    message.attempt,
                    message.available_at,
                    message.claimed_by,
                    message.lease_expires_at,
                    message.last_error,
                    message.created_at,
                    message.updated_at,
                ),
            ).fetchone()
        return _outbox(row)

    def claim_outbox(self, worker_id: str, lease_seconds: int = 60) -> Optional[OutboxMessage]:
        with self.database.connection() as connection:
            connection.execute(
                """UPDATE outbox_message SET status='pending', claimed_by=NULL,
                    lease_expires_at=NULL, updated_at=now()
                WHERE status='sending' AND lease_expires_at <= now()"""
            )
            row = connection.execute(
                """WITH candidate AS (
                    SELECT id FROM outbox_message
                    WHERE status='pending' AND available_at <= now()
                    ORDER BY available_at, created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE outbox_message AS message SET status='sending', claimed_by=%s,
                    attempt=message.attempt + 1,
                    lease_expires_at=now() + make_interval(secs => %s), updated_at=now()
                FROM candidate WHERE message.id=candidate.id RETURNING message.*""",
                (worker_id, lease_seconds),
            ).fetchone()
        return _outbox(row)

    def mark_outbox_sent(self, message_id: str, worker_id: str) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                """UPDATE outbox_message SET status='sent', claimed_by=NULL,
                    lease_expires_at=NULL, updated_at=now()
                WHERE id=%s AND claimed_by=%s AND status='sending' RETURNING id""",
                (message_id, worker_id),
            ).fetchone()
        return row is not None

    def retry_outbox(self, message_id: str, worker_id: str, error: str, delay_seconds: int) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                """UPDATE outbox_message SET status='pending', claimed_by=NULL,
                    lease_expires_at=NULL, last_error=%s,
                    available_at=now() + make_interval(secs => %s), updated_at=now()
                WHERE id=%s AND claimed_by=%s AND status='sending' RETURNING id""",
                (error, delay_seconds, message_id, worker_id),
            ).fetchone()
        return row is not None

    def fail_outbox(self, message_id: str, worker_id: str, error: str) -> bool:
        with self.database.connection() as connection:
            row = connection.execute(
                """UPDATE outbox_message SET status='failed', claimed_by=NULL,
                    lease_expires_at=NULL, last_error=%s, updated_at=now()
                WHERE id=%s AND claimed_by=%s AND status='sending' RETURNING id""",
                (error, message_id, worker_id),
            ).fetchone()
        return row is not None

    @staticmethod
    def _event(row) -> ExternalEvent:
        return ExternalEvent(
            channel=row["channel"],
            tenant_key=row["tenant_key"],
            external_event_id=row["external_event_id"],
            event_type=row["event_type"],
            payload_hash=row["payload_hash"],
            issue_id=row["issue_id"],
            created_at=row["created_at"],
        )
