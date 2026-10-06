import os
import threading
import time
from pathlib import Path

import pytest

from mica.application import RuntimeDiscoveryService
from mica.bootstrap import build_postgres_container
from mica.domain import Runtime, RuntimeCapability, utc_now
from mica.infrastructure import (
    InMemoryRuntimeCapabilityRepository,
    InMemoryRuntimeRepository,
)


def _executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_probe_records_only_available_local_providers(tmp_path: Path):
    codex = _executable(
        tmp_path / "codex",
        "#!/bin/sh\nprintf 'codex-cli 1.2.3\\n'\n",
    )
    runtimes = InMemoryRuntimeRepository()
    capabilities = InMemoryRuntimeCapabilityRepository()
    runtimes.add(Runtime(id="mac", name="Developer Mac"))
    discovery = RuntimeDiscoveryService(
        runtimes,
        capabilities,
        environ={
            "MICA_CODEX_PATH": str(codex),
            "MICA_CLAUDE_PATH": str(tmp_path / "missing-claude"),
            "MICA_PI_PATH": str(tmp_path / "missing-pi"),
            "MICA_DAEMON_ID": "daemon-mac",
        },
    )

    detected = discovery.probe("mac")

    assert [(item.provider, item.version) for item in detected] == [
        ("codex", "codex-cli 1.2.3")
    ]
    assert capabilities.get("mac", "codex").executable_path == str(codex)
    assert capabilities.get("mac", "claude") is None
    runtime = runtimes.get("mac")
    assert runtime.daemon_id == "daemon-mac"
    assert runtime.hostname
    assert runtime.platform
    assert runtime.last_seen_at is not None


def test_probe_replaces_stale_capabilities(tmp_path: Path):
    cli = _executable(tmp_path / "codex", "#!/bin/sh\necho codex-1\n")
    runtimes = InMemoryRuntimeRepository()
    capabilities = InMemoryRuntimeCapabilityRepository()
    runtimes.add(Runtime(id="mac", name="Developer Mac"))
    discovery = RuntimeDiscoveryService(
        runtimes,
        capabilities,
        environ={
            "MICA_CODEX_PATH": str(cli),
            "MICA_CLAUDE_PATH": str(tmp_path / "missing-claude"),
            "MICA_PI_PATH": str(tmp_path / "missing-pi"),
        },
    )
    discovery.probe("mac")
    cli.unlink()

    assert discovery.probe("mac") == []
    assert capabilities.list("mac") == []


@pytest.mark.skipif(
    not os.getenv("MICA_TEST_DATABASE_URL"), reason="requires a disposable PostgreSQL database"
)
def test_concurrent_probe_replace_converges_without_unique_violation():
    """API and daemon probe at startup; the overlapping replace must upsert, not crash."""
    container = build_postgres_container(
        os.environ["MICA_TEST_DATABASE_URL"], default_provider="stub"
    )
    runtime_id = os.environ.get("MICA_RUNTIME_ID", "local")
    repo = container.runtime_capabilities

    def capabilities(path: str) -> list[RuntimeCapability]:
        return [
            RuntimeCapability(
                runtime_id=runtime_id,
                provider="codex",
                executable_path=path,
                version="1.0",
                status="available",
                error=None,
                detected_at=utc_now(),
            )
        ]

    repo.replace_for_runtime(runtime_id, capabilities("/seed/codex"))

    inserted = threading.Event()
    errors = []

    def slow_replace() -> None:
        # Another process mid-probe: its delete + plain insert stay uncommitted
        # while this thread's replace_for_runtime runs.
        with container.database.transaction():
            with container.database.connection() as connection:
                connection.execute(
                    "DELETE FROM runtime_capability WHERE runtime_id=%s", (runtime_id,)
                )
                connection.execute(
                    """INSERT INTO runtime_capability(
                        runtime_id, provider, executable_path, version,
                        status, error, detected_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (runtime_id, "codex", "/slow/codex", "1.0", "available", None, utc_now()),
                )
                inserted.set()
                time.sleep(0.5)

    thread = threading.Thread(target=slow_replace)
    thread.start()
    assert inserted.wait(5)
    try:
        repo.replace_for_runtime(runtime_id, capabilities("/fast/codex"))
    except Exception as exc:  # surface the failure text, not just its type
        errors.append(exc)
    thread.join(5)

    assert not errors
    stored = repo.list(runtime_id)
    assert [(item.provider, item.executable_path) for item in stored] == [
        ("codex", "/fast/codex")
    ]
