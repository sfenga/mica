from __future__ import annotations

import os
import platform as platform_module
import shutil
import socket
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Optional

from mica.domain import RuntimeCapability, utc_now

from .ports import RuntimeCapabilityRepository, RuntimeRepository
from .services import NotFoundError


class RuntimeDiscoveryService:
    """Discovers executable agent providers on the Daemon host."""

    COMMANDS = {
        "codex": ("MICA_CODEX_PATH", "codex"),
        "claude": ("MICA_CLAUDE_PATH", "claude"),
        "pi": ("MICA_PI_PATH", "pi"),
    }

    def __init__(
        self,
        runtimes: RuntimeRepository,
        capabilities: RuntimeCapabilityRepository,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.runtimes = runtimes
        self.capabilities = capabilities
        self.environ = environ if environ is not None else os.environ

    def probe(self, runtime_id: str) -> list[RuntimeCapability]:
        runtime = self.runtimes.get(runtime_id)
        if runtime is None:
            raise NotFoundError(f"runtime not found: {runtime_id}")
        detected = []
        for provider, (env_name, command) in self.COMMANDS.items():
            configured = self.environ.get(env_name, "").strip()
            executable = self._resolve(configured or command, allow_shell=not configured)
            if executable is None:
                continue
            version, error = self._version(executable)
            detected.append(
                RuntimeCapability(
                    runtime_id=runtime_id,
                    provider=provider,
                    executable_path=executable,
                    version=version,
                    status="available" if error is None else "unavailable",
                    error=error,
                )
            )
        self.capabilities.replace_for_runtime(runtime_id, detected)
        self.runtimes.add(
            replace(
                runtime,
                daemon_id=self.environ.get("MICA_DAEMON_ID", runtime.daemon_id),
                status="online",
                hostname=socket.gethostname(),
                platform=platform_module.platform(),
                last_seen_at=utc_now(),
            )
        )
        return detected

    def _resolve(self, command: str, allow_shell: bool) -> Optional[str]:
        resolved = shutil.which(command)
        if resolved:
            return str(Path(resolved).resolve())
        path = Path(command).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        if allow_shell and "/" not in command and "\\" not in command:
            shell = self.environ.get("SHELL", "/bin/zsh")
            try:
                result = subprocess.run(
                    [shell, "-lic", f"command -v {command}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                candidate = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
                if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
                    return str(Path(candidate).resolve())
            except (OSError, subprocess.SubprocessError):
                pass
        if command == "codex":
            bundled = Path("/Applications/Codex.app/Contents/Resources/codex")
            if bundled.is_file() and os.access(bundled, os.X_OK):
                return str(bundled)
        return None

    @staticmethod
    def _version(executable: str) -> tuple[str, Optional[str]]:
        try:
            result = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return "", str(exc)
        output = (result.stdout or result.stderr).strip().splitlines()
        if result.returncode != 0:
            return "", output[-1] if output else f"version probe exited {result.returncode}"
        return (output[0] if output else "unknown"), None
