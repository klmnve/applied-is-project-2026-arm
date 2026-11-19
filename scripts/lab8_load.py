"""Run the four assigned Locust scenarios against a new synthetic local server.

Install requirements.load.txt in a separate virtual environment, then run this
script with that environment's Python. No URL or external target is accepted.
Temporary credentials, sessions and SQLite data are deleted after the run.
"""

import argparse
import csv
import importlib.metadata
import json
import os
import platform
import secrets
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib
import psutil
import requests

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/labs/lab8"
SCENARIOS = ((5, 60), (20, 120), (50, 180), (100, 180))


def save_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def csv_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_server(host: str, run_id: str, process: subprocess.Popen[str]) -> None:
    with requests.Session() as client:
        client.trust_env = False
        for _ in range(240):
            if process.poll() is not None:
                raise RuntimeError("Dedicated load server exited during startup")
            try:
                response = client.get(host + "/api/health", timeout=1)
                if response.headers.get("X-Lab8-Isolated-Run") == run_id:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.25)
    raise RuntimeError("Dedicated load server did not become ready")


def authenticated_sessions(host: str, password: str) -> list[dict[str, str]]:
    sessions: list[dict[str, str]] = []
    for index in range(100):
        with requests.Session() as client:
            client.trust_env = False
            response = client.post(
                host + "/api/auth/login",
                json={"login": "load_synthetic", "password": password},
                timeout=30,
            )
            if response.status_code != 200:
                raise RuntimeError(f"Preauthentication returned {response.status_code}")
            sessions.append(client.cookies.get_dict())
        if (index + 1) % 25 == 0:
            print(f"Prepared {index + 1}/100 real authenticated sessions", flush=True)
    return sessions


def actual_process(pid: int, marker: str) -> Any:
    """Windows venv python.exe may be a waiting launcher, not the interpreter."""
    root = psutil.Process(pid)
    for _ in range(20):
        matches = [
            process
            for process in root.children(recursive=True)
            if marker in " ".join(process.cmdline())
        ]
        if matches:
            return matches[-1]
        if os.name != "nt":
            return root
        time.sleep(0.1)
    return root


def terminate_tree(process: subprocess.Popen[str]) -> None:
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for child in reversed(children):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    process.terminate()
    process.wait(timeout=15)
    psutil.wait_procs(children, timeout=15)


def run_scenario(users: int, seconds: int, context_file: Path, server_pid: int) -> dict[str, Any]:
    prefix = OUTPUT / f"load-{users:03}"
    # A fresh temporary result cannot be confused with an earlier published run.
    result_path = context_file.parent / f"result-{users}.json"
    environment = os.environ.copy()
    environment.update(
        {
            "LAB8_LOAD_CONTEXT": str(context_file),
            "LAB8_LOAD_RESULT": str(result_path),
            "PYTHONUNBUFFERED": "1",
        }
    )
    command = [
        sys.executable,
        "-m",
        "locust",
        "-f",
        str(ROOT / "scripts/lab8_load_locust.py"),
        "--headless",
        "--users",
        str(users),
        "--spawn-rate",
        str(users),
        "--run-time",
        f"{seconds}s",
        "--stop-timeout",
        "15",
        "--only-summary",
        "--csv",
        str(prefix),
        "--csv-full-history",
        "--loglevel",
        "WARNING",
    ]
    server = actual_process(server_pid, "lab8_load_server.py")
    samples: list[dict[str, Any]] = []
    server.cpu_percent(None)
    psutil.cpu_percent(None)
    print(f"Starting {users} users for {seconds} seconds", flush=True)
    with prefix.with_suffix(".log").open("w", encoding="utf-8") as log:
        worker = subprocess.Popen(
            command, cwd=ROOT, env=environment, stdout=log, stderr=log, text=True
        )
        generator = actual_process(worker.pid, "locust")
        generator.cpu_percent(None)
        started = time.monotonic()
        while worker.poll() is None:
            time.sleep(1)
            try:
                samples.append(
                    {
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "server_cpu_percent_one_core": server.cpu_percent(None),
                        "server_rss_mib": round(server.memory_info().rss / 2**20, 3),
                        "system_cpu_percent": psutil.cpu_percent(None),
                        "system_memory_used_mib": round(psutil.virtual_memory().used / 2**20, 3),
                        "generator_cpu_percent_one_core": generator.cpu_percent(None),
                        "generator_rss_mib": round(generator.memory_info().rss / 2**20, 3),
                    }
                )
            except psutil.NoSuchProcess:
                break
            if time.monotonic() - started > seconds + 60:
                terminate_tree(worker)
                raise RuntimeError("Locust exceeded its bounded run time")
        exit_code = worker.wait(timeout=10)
    if not result_path.exists():
        raise RuntimeError(f"Locust did not produce {result_path.name}; inspect its log")
    result: dict[str, Any] = json.loads(result_path.read_text(encoding="utf-8"))
    csv_rows(OUTPUT / f"load-{users:03}-resources.csv", samples)
    result.update(
        {
            "users": users,
            "configured_seconds": seconds,
            "locust_exit_code": exit_code,
            "server_cpu_mean_percent_one_core": statistics.mean(
                row["server_cpu_percent_one_core"] for row in samples
            ),
            "server_cpu_max_percent_one_core": max(
                row["server_cpu_percent_one_core"] for row in samples
            ),
            "server_rss_mean_mib": statistics.mean(row["server_rss_mib"] for row in samples),
            "server_rss_max_mib": max(row["server_rss_mib"] for row in samples),
            "system_cpu_mean_percent": statistics.mean(
                row["system_cpu_percent"] for row in samples
            ),
            "generator_cpu_mean_percent_one_core": statistics.mean(
                row["generator_cpu_percent_one_core"] for row in samples
            ),
            "resource_samples": len(samples),
        }
    )
    save_json(prefix.with_suffix(".json"), result)
    print(
        f"Completed {users}: {result['requests']} requests, {result['failures']} errors, "
        f"mean {result['mean_ms']:.1f} ms, p95 {result['p95_ms']:.1f} ms",
        flush=True,
    )
    return result


def plot(results: list[dict[str, Any]]) -> None:
    labels = [str(row["users"]) for row in results]
    fig, axes = plt.subplots(2, 2, figsize=(11, 7), constrained_layout=True)
    axes[0, 0].plot(labels, [row["mean_ms"] for row in results], "o-", label="Mean")
    axes[0, 0].plot(labels, [row["p95_ms"] for row in results], "o-", label="p95")
    axes[0, 0].set_ylabel("Response time (ms)")
    axes[0, 0].legend()
    axes[0, 1].bar(labels, [row["requests_per_second"] for row in results], color="#176b5b")
    axes[0, 1].set_ylabel("Throughput (requests/s)")
    axes[1, 0].plot(labels, [row["server_cpu_mean_percent_one_core"] for row in results], "o-")
    axes[1, 0].set_ylabel("Server CPU (% of one logical core)")
    axes[1, 1].plot(labels, [row["server_rss_max_mib"] for row in results], "o-")
    axes[1, 1].set_ylabel("Server peak RSS (MiB)")
    for axis in axes.flat:
        axis.set_xlabel("Concurrent virtual users")
        axis.grid(axis="y", alpha=0.2)
    fig.suptitle("Lab 8 - paced authenticated reads on isolated local FastAPI")
    fig.savefig(OUTPUT / "load-comparison.png", dpi=170)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    default_python = ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    parser.add_argument("--server-python", type=Path, default=default_python)
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    started_utc = datetime.now(UTC).isoformat()
    environment_details = {
        "os": platform.platform(),
        "cpu": platform.processor(),
        "logical_cpus": psutil.cpu_count(),
        "physical_cpus": psutil.cpu_count(logical=False),
        "system_ram_mib": round(psutil.virtual_memory().total / 2**20, 1),
        "generator_python": platform.python_version(),
        "locust": importlib.metadata.version("locust"),
        "server": "one Uvicorn process, default asyncio on Windows, SQLite",
        "think_time_seconds": [1, 3],
        "spawn_rate": "all users within approximately 1 second",
        "authentication": "100 real sessions for one synthetic administrator, login before timing",
        "measurement": "request latency includes HTTP+authorization+DB; read-only business routes",
        "cpu_units": (
            "server process 100% equals one logical CPU; system CPU normalized over all CPUs"
        ),
        "isolation": "new temporary SQLite and uploads; loopback only; no production data",
        "limitations": (
            "load generator and server share one workstation; not capacity certification"
        ),
    }
    results = []
    with tempfile.TemporaryDirectory(prefix="arm-lab8-load-") as temp:
        directory = Path(temp)
        password, run_id, port = secrets.token_urlsafe(32), secrets.token_hex(16), free_port()
        host = f"http://127.0.0.1:{port}"
        with (OUTPUT / "load-server.log").open("w", encoding="utf-8") as log:
            server = subprocess.Popen(
                [str(args.server_python), str(ROOT / "scripts/lab8_load_server.py")],
                cwd=ROOT,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
                text=True,
            )
            try:
                assert server.stdin is not None
                server.stdin.write(
                    json.dumps(
                        {
                            "directory": str(directory),
                            "password": password,
                            "run_id": run_id,
                            "port": port,
                        }
                    )
                )
                server.stdin.close()
                wait_server(host, run_id, server)
                sessions = authenticated_sessions(host, password)
                context = directory / "session-context.json"
                save_json(context, {"host": host, "run_id": run_id, "sessions": sessions})
                if os.name != "nt":
                    context.chmod(0o600)
                for users, seconds in SCENARIOS:
                    results.append(run_scenario(users, seconds, context, server.pid))
                    save_json(
                        OUTPUT / "load-summary.json",
                        {
                            "started_utc": started_utc,
                            "environment": environment_details,
                            "scenarios": results,
                        },
                    )
            finally:
                terminate_tree(server)
    csv_rows(
        OUTPUT / "load-summary.csv",
        [
            {key: value for key, value in row.items() if not isinstance(value, (dict, list))}
            for row in results
        ],
    )
    plot(results)
    print("Finished all four assigned load scenarios; artifacts: docs/labs/lab8/load-*", flush=True)


if __name__ == "__main__":
    main()
