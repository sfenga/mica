import json
import os
from uuid import uuid4

import pytest

from mica.application import IngressCommand
from mica.bootstrap import build_postgres_container
from mica.domain import Agent, AssigneeType


class RollbackSmokeTest(Exception):
    pass


@pytest.mark.skipif(not os.getenv("MICA_TEST_DATABASE_URL"), reason="requires a disposable PostgreSQL database")
def test_postgres_ingress_is_atomic_and_idempotent():
    container = build_postgres_container(os.environ["MICA_TEST_DATABASE_URL"], default_provider="stub")
    event_id = f"integration-{uuid4().hex}"
    issue_id = None

    try:
        with container.database.transaction():
            command = IngressCommand(
                channel="web",
                tenant_key="integration-workspace",
                external_event_id=event_id,
                external_user_id="integration-user",
                display_name="Integration User",
                conversation_id="integration-user",
                title="PostgreSQL channel smoke test",
                body="This transaction must roll back after assertions.",
                workspace_id="integration-workspace",
                assignee_id="local-assistant",
            )
            first, created = container.ingress_service.submit(command)
            duplicate, duplicate_created = container.ingress_service.submit(command)
            issue_id = first.id

            assert created is True
            assert duplicate_created is False
            assert duplicate.id == first.id
            assert len(container.tasks.list(first.id)) == 1
            raise RollbackSmokeTest
    except RollbackSmokeTest:
        pass

    assert container.issues.get(issue_id) is None
    with container.database.connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM external_event WHERE external_event_id=%s", (event_id,)
        ).fetchone()
    assert row is None


@pytest.mark.skipif(not os.getenv("MICA_TEST_DATABASE_URL"), reason="requires a disposable PostgreSQL database")
def test_postgres_squad_routing_persists_task_provenance():
    container = build_postgres_container(os.environ["MICA_TEST_DATABASE_URL"])
    suffix = uuid4().hex
    issue_id = None
    try:
        with container.database.transaction():
            squad = container.squad_service.create(
                name=f"integration-squad-{suffix}",
                workspace_id="integration-workspace",
                leader_agent_id="local-assistant",
                members=[],
            )
            issue = container.trigger_service.create_issue(
                title="PostgreSQL squad routing",
                body="Verify leader task provenance.",
                workspace_id="integration-workspace",
                assignee_type=AssigneeType.SQUAD,
                assignee_id=squad.id,
            )
            issue_id = issue.id
            task = container.tasks.list(issue.id)[0]
            assert task.agent_id == "local-assistant"
            assert task.squad_id == squad.id
            assert task.is_leader_task is True
            raise RollbackSmokeTest
    except RollbackSmokeTest:
        pass

    assert container.issues.get(issue_id) is None


@pytest.mark.skipif(
    not os.getenv("MICA_TEST_DATABASE_URL"), reason="requires a disposable PostgreSQL database"
)
def test_postgres_squad_follow_up_waits_for_delegated_worker():
    container = build_postgres_container(os.environ["MICA_TEST_DATABASE_URL"])
    suffix = uuid4().hex
    leader_id = f"leader-{suffix}"
    worker_id = f"worker-{suffix}"
    try:
        with container.database.transaction():
            container.agents.add(Agent(leader_id, "Leader", "local", "stub"))
            container.agents.add(Agent(worker_id, "Worker", "local", "stub"))
            squad = container.squad_service.create(
                name=f"ordered-squad-{suffix}",
                workspace_id="integration-workspace",
                leader_agent_id=leader_id,
                members=[{"agent_id": worker_id}],
            )
            issue = container.trigger_service.create_issue(
                title="ordered follow-up",
                body="delegate work",
                workspace_id="integration-workspace",
                assignee_type=AssigneeType.SQUAD,
                assignee_id=squad.id,
            )
            container.trigger_service.add_comment(issue.id, "new constraint", "user-1")

            first = container.task_service.claim_next("local", "test-daemon")
            assert first.is_leader_task
            container.task_service.start(first.id, "test-daemon", first.attempt)
            container.squad_service.complete_task(
                first,
                json.dumps(
                    {
                        "decision": "delegate",
                        "summary": "delegate",
                        "assignments": [
                            {"agent_id": worker_id, "instruction": "check constraint"}
                        ],
                    }
                ),
                None,
                "test-daemon",
                first.attempt,
            )

            second = container.task_service.claim_next("local", "test-daemon")
            assert second.agent_id == worker_id
            assert not second.is_leader_task
            raise RollbackSmokeTest
    except RollbackSmokeTest:
        pass


@pytest.mark.skipif(
    not os.getenv("MICA_TEST_DATABASE_URL"), reason="requires a disposable PostgreSQL database"
)
def test_postgres_merges_queued_comments_and_snapshots_them_when_claimed():
    container = build_postgres_container(os.environ["MICA_TEST_DATABASE_URL"])
    try:
        with container.database.transaction():
            issue = container.trigger_service.create_issue(
                title="merge follow-ups",
                body="inspect the current implementation",
                workspace_id="integration-workspace",
                assignee_id="local-assistant",
            )
            first = container.trigger_service.add_comment(issue.id, "first detail", "user-1")
            second = container.trigger_service.add_comment(issue.id, "second detail", "user-1")

            queued = container.tasks.list(issue.id)
            assert len(queued) == 1
            assert queued[0].merged_comment_ids == [first.id]
            assert queued[0].trigger_comment_id == second.id

            claimed = container.task_service.claim_next("local", "test-daemon")
            assert claimed.delivered_comment_ids == [first.id, second.id]
            raise RollbackSmokeTest
    except RollbackSmokeTest:
        pass
