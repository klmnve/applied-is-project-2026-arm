"""Private subprocess entry point for the isolated Lab 8 measurement server."""

import json
import sys
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402
from fastapi import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402

from app.bootstrap import create_admin  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.store import Store  # noqa: E402


def main() -> None:
    inputs = json.load(sys.stdin)
    directory = Path(inputs["directory"]).resolve()
    if directory.parent != Path(tempfile.gettempdir()).resolve() or not directory.name.startswith(
        "arm-lab8-load-"
    ):
        raise ValueError("Load server accepts only its dedicated temporary directory")
    # BaseSettings supports this runtime override; its generated mypy signature omits it.
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_path=directory / "synthetic.sqlite3",
        upload_dir=directory / "uploads",
        trusted_proxies="",
        cookie_secure=False,
    )
    create_admin(Store(settings), "load_synthetic", "Synthetic load reader", inputs["password"])
    application = create_app(settings)

    @application.middleware("http")
    async def identify_isolated_run(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Lab8-Isolated-Run"] = inputs["run_id"]
        return response

    uvicorn.run(
        application,
        host="127.0.0.1",
        port=int(inputs["port"]),
        access_log=False,
        log_level="warning",
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
