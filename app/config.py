"""Non-secret configuration for local models and the SQLite database."""

from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_path: Path = Path("data/audit.db")
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = Field(default="qwen2.5:1.5b", min_length=1)
    embedding_model: str = Field(default="models/multilingual-e5-small", min_length=1)
    embedding_prefix: str = Field(default="query: ", max_length=100)
    semantic_threshold: float = Field(default=0.80, ge=-1, le=1, allow_inf_nan=False)
    llm_timeout_seconds: float = Field(default=180, gt=0, le=600, allow_inf_nan=False)
    max_candidates: int = Field(default=500, ge=1, le=10000)
    upload_dir: Path = Path("data/uploads")
    max_upload_bytes: int = Field(default=5 * 1024 * 1024, ge=1, le=50 * 1024 * 1024)
    cookie_secure: bool = False
    session_ttl_seconds: int = Field(default=28800, ge=60, le=604800)
    login_max_attempts: int = Field(default=5, ge=1, le=100)
    login_window_seconds: int = Field(default=300, ge=1, le=86400)
    suspicious_failure_threshold: int = Field(default=3, ge=1, le=100)
    suspicious_window_seconds: int = Field(default=600, ge=1, le=86400)
    allowed_origins: str = "http://127.0.0.1:8000,http://localhost:8000"
    trusted_proxies: str = ""
    student_email_required: bool = False
    student_email_domain: str = ""

    @field_validator("student_email_domain")
    @classmethod
    def valid_student_email_domain(cls, value: str) -> str:
        # Keep configuration validation independent from the application claim module.
        import re

        domain = value.strip().lower()
        if domain and (
            len(domain) > 253
            or "." not in domain
            or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                for part in domain.split(".")
            )
        ):
            raise ValueError("Invalid student email domain")
        return domain

    @field_validator("trusted_proxies")
    @classmethod
    def valid_trusted_proxies(cls, value: str) -> str:
        from ipaddress import ip_network

        for network in value.split(","):
            if network.strip():
                ip_network(network.strip(), strict=False)
        return value

    @field_validator("ollama_base_url")
    @classmethod
    def local_ollama_url(cls, value: str) -> str:
        """Only an explicit loopback server may receive audit data."""
        value = value.strip()
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "OLLAMA_BASE_URL must be an HTTP(S) loopback origin without credentials"
            )
        host = parsed.hostname.lower()
        try:
            loopback = ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback:
            raise ValueError("OLLAMA_BASE_URL must use localhost or a loopback IP address")
        # Accessing .port also validates invalid/out-of-range ports.
        _ = parsed.port
        return value.rstrip("/")

    @field_validator("ollama_model", "embedding_model")
    @classmethod
    def nonempty_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Model name/path cannot be blank")
        return value
