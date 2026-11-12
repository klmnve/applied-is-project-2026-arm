"""Build and exercise real Compose services in a unique disposable project.

Run: python scripts/docker_smoke.py
Requires Docker Engine and Compose v2. Never mounts the working project's data.
The temporary admin password goes to container stdin only; credentials and tokens
are omitted from reports. Only this run's randomly named volume is removed.
"""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = """
import json, sys
from app.bootstrap import create_admin
from app.config import Settings
from app.store import Store
credentials = json.load(sys.stdin)
create_admin(Store(Settings()), credentials['login'], 'Container smoke administrator',
             credentials['password'])
print('Temporary administrator created')
"""
NETWORK_CHECK = """
import json, os, urllib.request
from app.frontend.client import APIClient
assert os.getuid() != 0, 'Container must not run as root'
url = os.environ['BACKEND_URL'] + '/api/health'
assert url == 'http://backend:8000/api/health'
with urllib.request.urlopen(url, timeout=10) as response:
    assert json.load(response)['status'] == 'alive'
print('Service DNS and backend HTTP verified')
"""
VOLUME_CHECK = """
import json, os
from pathlib import Path
from app.config import Settings
assert os.getuid() != 0, 'Container must not run as root'
settings = Settings()
assert str(settings.database_path) == '/workspace/data/audit.db'
assert str(settings.upload_dir) == '/workspace/data/uploads'
assert settings.database_path.is_file()
path = settings.upload_dir / 'docker-smoke-persistence.txt'
if os.environ.get('SMOKE_CHECK_EXISTING') == '1':
    assert path.read_text() == 'persistent synthetic marker'
else:
    path.write_text('persistent synthetic marker')
print(json.dumps({'nonroot': True, 'database_and_upload_volume': True}))
"""


class ComposeSmoke:
    def __init__(self, docker: str, env_file: Path, output: Path) -> None:
        self.project = "arm-smoke-" + uuid.uuid4().hex[:12]
        self.output = output
        self.prefix = [
            docker,
            "compose",
            "--project-name",
            self.project,
            "--project-directory",
            str(ROOT),
            "--env-file",
            str(env_file),
            "-f",
            str(ROOT / "docker-compose.yml"),
        ]
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "ARM_BACKEND_PORT": "0",
                "ARM_FRONTEND_PORT": "0",
                "PUBLIC_BACKEND_URL": "http://localhost:8000",
            }
        )
        self.secrets: list[str] = []
        self.checks: list[str] = []
        self.log: list[str] = []

    def redact(self, value: str) -> str:
        for secret in sorted(self.secrets, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value

    def run(
        self,
        *arguments: str,
        stdin: str | None = None,
        timeout: int = 180,
        required: bool = True,
    ) -> str:
        try:
            result = subprocess.run(
                [*self.prefix, *arguments],
                cwd=ROOT,
                env=self.environment,
                input=stdin,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Docker Compose {arguments[0]} timed out") from exc
        output = result.stdout + result.stderr
        self.log.append(f"--- compose {arguments[0]} ---\n" + self.redact(output))
        if required and result.returncode:
            raise RuntimeError(f"Docker Compose {arguments[0]} failed (exit {result.returncode})")
        return result.stdout.strip()

    def endpoint(self, service: str, port: int) -> str:
        address = self.run("port", service, str(port)).splitlines()[0]
        host, separator, published_port = address.rpartition(":")
        if not separator or host != "127.0.0.1" or not published_port.isdigit():
            raise RuntimeError("Smoke services must publish only loopback ports")
        return "http://" + address

    def check(self, label: str) -> None:
        self.checks.append(label)
        print("PASS: " + label, flush=True)

    def login(self, client: httpx.Client, login: str, password: str) -> None:
        response = client.post("/api/auth/login", json={"login": login, "password": password})
        if response.status_code != 200:
            raise RuntimeError(f"Container login returned HTTP {response.status_code}")
        token = str(response.json()["csrf_token"])
        self.secrets.extend([token, *client.cookies.values()])
        client.headers["X-CSRF-Token"] = token

    def exercise(self) -> None:
        self.run("config", "--quiet")
        startup = ["up", "-d", "--wait", "--wait-timeout", "240", "--build"]
        self.run(*startup, timeout=1200)
        self.check("both Compose services start healthy")
        self.run("exec", "-T", "frontend", "python", "-c", NETWORK_CHECK)
        self.run("exec", "-T", "backend", "python", "-c", VOLUME_CHECK)
        self.check("nonroot containers and internal service DNS")
        login, password = "container_smoke_admin", secrets.token_urlsafe(32)
        self.secrets.append(password)
        self.run(
            "exec",
            "-T",
            "backend",
            "python",
            "-c",
            BOOTSTRAP,
            stdin=json.dumps({"login": login, "password": password}),
        )
        backend = self.endpoint("backend", 8000)
        frontend = self.endpoint("frontend", 8501)
        with httpx.Client(base_url=backend, timeout=30, trust_env=False) as client:
            assert client.get("/api/health").json()["status"] == "alive"
            assert client.get("/docs").status_code == 200
            assert client.get("/openapi.json").status_code == 200
            assert client.get("/api/resources").status_code == 401
            self.login(client, login, password)
            categories = client.get("/api/resource-categories").json()["items"]
            rooms = client.get("/api/resource-rooms").json()["items"]
            seeded = client.get("/api/resources").json()["items"]
            assert len(categories) == 3 and len(rooms) == 3 and len(seeded) == 4
            payload = {
                "name": "Container persistence probe",
                "inventory_code": "DOCKER-SMOKE-001",
                "category_id": categories[0]["id"],
                "room_id": rooms[0]["id"],
                "quantity": 2,
                "unit_cost": 100,
            }
            csrf = client.headers.pop("X-CSRF-Token")
            assert client.post("/api/resources", json=payload).status_code == 403
            client.headers["X-CSRF-Token"] = csrf
            assert client.post("/api/resources", json=payload | {"id": 99}).status_code == 422
            assert client.post("/api/resources", json=payload | {"quantity": -1}).status_code == 422
            created = client.post("/api/resources", json=payload)
            assert created.status_code == 201
            resource_id = created.json()["item"]["id"]
            assert client.post("/api/resources", json=payload).status_code == 409
            updated = client.patch(f"/api/resources/{resource_id}", json={"quantity": 7})
            assert updated.status_code == 200 and updated.json()["item"]["quantity"] == 7
            filtered = client.get("/api/resources", params={"search": "persistence probe"})
            assert filtered.status_code == 200 and filtered.json()["total"] == 1
            deleted_seed_id = seeded[0]["id"]
            assert client.delete(f"/api/resources/{deleted_seed_id}").status_code == 204
            assert client.get(f"/api/resources/{deleted_seed_id}").status_code == 404
        with httpx.Client(timeout=30, trust_env=False) as browser:
            assert browser.get(frontend + "/_stcore/health").status_code == 200
            assert browser.get(frontend + "/").status_code == 200
        self.check("real HTTP login, CSRF, CRUD, search, validation and frontend health")
        # Deliberately retain this run's named volume while removing its containers.
        self.run("down", "--timeout", "30")
        self.run("up", "-d", "--wait", "--wait-timeout", "240", timeout=360)
        self.run(
            "exec", "-T", "-e", "SMOKE_CHECK_EXISTING=1", "backend", "python", "-c", VOLUME_CHECK
        )
        with httpx.Client(
            base_url=self.endpoint("backend", 8000), timeout=30, trust_env=False
        ) as client:
            self.login(client, login, password)
            retained = client.get(f"/api/resources/{resource_id}")
            assert retained.status_code == 200 and retained.json()["item"]["quantity"] == 7
            assert client.get(f"/api/resources/{deleted_seed_id}").status_code == 404
            assert client.delete(f"/api/resources/{resource_id}").status_code == 204
        self.run("exec", "-T", "frontend", "python", "-c", NETWORK_CHECK)
        self.check("SQLite, upload files and deleted seed state persist across down and up")

    def report(self, status: str, error: str | None) -> None:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        report: dict[str, Any] = {
            "status": status,
            "checked_at_utc": datetime.now(UTC).isoformat(),
            "isolated_compose_project": self.project,
            "checks": self.checks,
            "uses_working_project_database": False,
            "credential_transport": "stdin only; random credentials omitted",
        }
        if error is not None:
            report["error"] = self.redact(error)
        self.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.output.with_suffix(".log").write_text(
            self.redact("\n".join(self.log)), encoding="utf-8"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/docker/smoke-result.json")
    args = parser.parse_args()
    docker = shutil.which("docker")
    if docker is None:
        print(
            "Docker CLI is unavailable. Install Docker with Compose v2 and rerun.", file=sys.stderr
        )
        return 2
    with tempfile.TemporaryDirectory(prefix="arm-docker-smoke-") as temporary:
        env_file = Path(temporary) / "empty.env"
        env_file.write_text("", encoding="utf-8")
        smoke = ComposeSmoke(docker, env_file, args.output)
        status, error = "passed", None
        try:
            smoke.exercise()
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            print(smoke.redact(error), file=sys.stderr)
            try:
                smoke.run("logs", "--no-color", "--tail", "150", required=False)
            except RuntimeError as log_error:
                smoke.log.append(str(log_error))
        finally:
            # The unpredictable project name ensures no user's volume is targeted.
            try:
                smoke.run("down", "--volumes", "--remove-orphans", "--timeout", "30")
            except RuntimeError as cleanup_error:
                status = "failed"
                error = (error + "; " if error else "") + str(cleanup_error)
            smoke.report(status, error)
        print(f"Docker evidence: {args.output}")
        return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
