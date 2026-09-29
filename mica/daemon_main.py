from __future__ import annotations

import logging
import os
import signal
from threading import Event, Thread

from mica.bootstrap import build_container_from_env


def main() -> None:
    logging.basicConfig(
        level=os.getenv("MICA_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    container = build_container_from_env()
    stopped = Event()

    def stop(*_args) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    container.worker.start()
    runtime_id = os.getenv("MICA_RUNTIME_ID", "local")

    def discover_runtimes() -> None:
        interval = float(os.getenv("MICA_RUNTIME_DISCOVERY_INTERVAL", "120"))
        while not stopped.is_set():
            try:
                container.runtime_discovery.probe(runtime_id)
            except Exception:
                logging.exception("runtime discovery failed")
            stopped.wait(interval)

    discovery_thread = Thread(target=discover_runtimes, daemon=True)
    discovery_thread.start()
    try:
        stopped.wait()
    finally:
        stopped.set()
        discovery_thread.join(timeout=2)
        container.worker.stop()


if __name__ == "__main__":
    main()
