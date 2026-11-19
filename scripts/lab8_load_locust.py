"""Paced authenticated reads; usable only through lab8_load.py's private context."""

import itertools
import json
import os
import random
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import gevent
import requests
from locust import HttpUser, between, events, task

CONTEXT = json.loads(Path(os.environ["LAB8_LOAD_CONTEXT"]).read_text(encoding="utf-8"))
TARGET = str(CONTEXT["host"])
parsed = urlsplit(TARGET)
if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path:
    raise ValueError("Only the temporary loopback server is allowed")
SESSION_IDS = itertools.count()
STATUSES: Counter[str] = Counter()


@events.init.add_listener  # type: ignore[untyped-decorator]
def confirm_isolated_server(environment: Any, **kwargs: Any) -> None:
    if environment.host and environment.host != TARGET:
        raise ValueError("An arbitrary Locust target is not allowed")
    environment.host = TARGET
    with requests.Session() as probe:
        probe.trust_env = False
        response = probe.get(TARGET + "/api/health", timeout=5, allow_redirects=False)
        if response.headers.get("X-Lab8-Isolated-Run") != CONTEXT["run_id"]:
            raise ValueError("Target is not the dedicated synthetic server")


@events.request.add_listener  # type: ignore[untyped-decorator]
def count_status(response: Any = None, exception: Any = None, **kwargs: Any) -> None:
    STATUSES[str(response.status_code) if response is not None else "connection_error"] += 1


@events.quitting.add_listener  # type: ignore[untyped-decorator]
def save_measurements(environment: Any, **kwargs: Any) -> None:
    total = environment.stats.total
    result = {
        "requests": total.num_requests,
        "failures": total.num_failures,
        "error_percent": total.fail_ratio * 100,
        "mean_ms": total.avg_response_time,
        "median_ms": total.get_response_time_percentile(0.50),
        "p95_ms": total.get_response_time_percentile(0.95),
        "p99_ms": total.get_response_time_percentile(0.99),
        "min_ms": total.min_response_time,
        "max_ms": total.max_response_time,
        "requests_per_second": total.total_rps,
        "measurement_start_epoch": environment.stats.start_time,
        "measurement_end_epoch": time.time(),
        "status_counts": dict(STATUSES),
        "errors": [
            {
                "method": error.method,
                "name": error.name,
                "occurrences": error.occurrences,
                "error": str(error.error),
            }
            for error in environment.stats.errors.values()
        ],
    }
    Path(os.environ["LAB8_LOAD_RESULT"]).write_text(json.dumps(result, indent=2), encoding="utf-8")


class DepartmentReader(HttpUser):  # type: ignore[misc]
    host = TARGET
    wait_time = between(1, 3)

    def on_start(self) -> None:
        self.client.trust_env = False
        index = next(SESSION_IDS)
        if index >= 100:
            raise ValueError("At most 100 virtual users are allowed")
        self.client.cookies.update(CONTEXT["sessions"][index])
        gevent.sleep(random.uniform(0, 2))

    def read(self, path: str, name: str) -> None:
        with self.client.get(
            path, name=name, timeout=15, allow_redirects=False, catch_response=True
        ) as response:
            if response.status_code != 200:
                response.failure(f"Expected 200; received {response.status_code}")
            elif response.headers.get("X-Lab8-Isolated-Run") != CONTEXT["run_id"]:
                response.failure("Isolation marker mismatch")
            else:
                try:
                    response.json()
                except ValueError:
                    response.failure("Expected a JSON response")

    @task(3)  # type: ignore[untyped-decorator]
    def catalog(self) -> None:
        self.read("/api/resources?page=1&page_size=25", "resources/list")

    @task(2)  # type: ignore[untyped-decorator]
    def search(self) -> None:
        self.read("/api/resources?search=DEMO&category_id=1", "resources/search")

    @task(2)  # type: ignore[untyped-decorator]
    def detail(self) -> None:
        self.read("/api/resources/1", "resources/detail")

    @task(1)  # type: ignore[untyped-decorator]
    def categories(self) -> None:
        self.read("/api/resource-categories", "resources/categories")

    @task(1)  # type: ignore[untyped-decorator]
    def rooms(self) -> None:
        self.read("/api/resource-rooms", "resources/rooms")

    @task(1)  # type: ignore[untyped-decorator]
    def dashboard(self) -> None:
        self.read("/api/dashboard", "dashboard")
