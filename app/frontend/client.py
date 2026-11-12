"""Per-user REST client for the existing ARM authentication and resource API."""

from typing import Any

import requests


class APIError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class APIClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "Accept-Language": "kk"})

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {}
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            csrf = self.session.cookies.get("arm_csrf")
            if csrf:
                headers["X-CSRF-Token"] = csrf
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                timeout=(5, 20),
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise APIError(
                "Серверге қосылу мүмкін болмады. Қосылымды тексеріп, қайта көріңіз. "
                "Өзгерісті қайталар алдында тізімді жаңартыңыз."
            ) from exc
        if response.status_code == 204:
            return None
        try:
            data = response.json()
        except ValueError as exc:
            raise APIError("Серверден күтпеген жауап келді.", response.status_code) from exc
        if not 200 <= response.status_code < 300:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            detail = data.get("detail") if isinstance(data, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            if not isinstance(message, str):
                message = detail if isinstance(detail, str) else None
            if response.status_code >= 500:
                message = "Сервер сұрауды өңдей алмады. Кейінірек қайталап көріңіз."
            raise APIError(message or "Сұрауды орындау мүмкін болмады.", response.status_code)
        return data

    def close(self) -> None:
        self.session.cookies.clear()
        self.session.close()
