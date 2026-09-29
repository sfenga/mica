import logging
import os

from mica.api import create_app

app = create_app()


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.getenv("MICA_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        "mica.main:app",
        host=os.getenv("MICA_API_HOST", "127.0.0.1"),
        port=_api_port(),
        reload=False,
    )


def _api_port() -> int:
    try:
        port = int(os.getenv("MICA_API_PORT", "8000"))
    except ValueError as exc:
        raise ValueError("MICA_API_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("MICA_API_PORT must be between 1 and 65535")
    return port


if __name__ == "__main__":
    main()
