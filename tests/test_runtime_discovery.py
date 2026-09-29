from pathlib import Path

from mica.application import RuntimeDiscoveryService
from mica.domain import Runtime
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
