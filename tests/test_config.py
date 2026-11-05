import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize(
    "url",
    [
        "https://external-model.example.com",
        "http://localhost:11434/path",
        "http://localhost:11434?route=external",
        "http://user:synthetic-password@localhost:11434",
    ],
)
def test_ollama_configuration_rejects_remote_or_credentialed_endpoints(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ollama_base_url=url)


@pytest.mark.parametrize(
    "url", ["http://localhost:11434", "http://127.0.0.1:11434", "http://[::1]:11434"]
)
def test_ollama_configuration_accepts_local_loopback_endpoints(url):
    assert Settings(_env_file=None, ollama_base_url=url).ollama_base_url == url
