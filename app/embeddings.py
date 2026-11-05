"""CPU multilingual embeddings from already downloaded local files only."""

import math
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from app.config import Settings
from app.errors import AppError


class EmbeddingAdapter(Protocol):
    def encode(self, texts: list[str]) -> list[list[float]]: ...

    def readiness(self) -> dict[str, str]: ...


def validate_vectors(vectors: list[list[float]], count: int) -> None:
    if len(vectors) != count or not vectors:
        raise ValueError("Invalid embedding count")
    size = len(vectors[0])
    for vector in vectors:
        squared_norm = sum(value * value for value in vector)
        if (
            not size
            or len(vector) != size
            or any(not math.isfinite(value) for value in vector)
            or not math.isfinite(squared_norm)
            or squared_norm <= 0
        ):
            raise ValueError("Invalid embedding vector")


class LocalEmbedder:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model: Any = None
        self._lock = RLock()

    def _load(self) -> Any:
        if self._model is None:
            model_path = Path(self.settings.embedding_model).resolve()
            if not model_path.is_dir():
                raise FileNotFoundError("Local embedding directory missing")
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                str(model_path),
                device="cpu",
                local_files_only=True,
                trust_remote_code=False,
                token=False,
            )
        return self._model

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            with self._lock:
                values = self._load().encode(
                    texts,
                    normalize_embeddings=True,
                    show_progress_bar=False,
                    convert_to_numpy=True,
                    batch_size=32,
                    prompt=self.settings.embedding_prefix,
                )
            vectors = [[float(value) for value in row] for row in values]
            validate_vectors(vectors, len(texts))
            return vectors
        except (
            OSError,
            ImportError,
            RuntimeError,
            ValueError,
            TypeError,
            AttributeError,
            OverflowError,
        ) as exc:
            raise AppError(
                "model_unavailable",
                "Локалды embedding моделі жүктелмеді. /api/readiness нұсқауын қараңыз.",
                503,
            ) from exc

    def readiness(self) -> dict[str, str]:
        setup = (
            "python -m pip install -e '.[models]'; "
            "python scripts/download_embedding.py --output " + self.settings.embedding_model
        )
        try:
            self.encode(["Локалды модель дайындығы"])
            return {"status": "ready", "message": "Локалды embedding дайын (CPU).", "setup": setup}
        except AppError:
            return {
                "status": "unavailable",
                "message": "Embedding файлы, тәуелділігі немесе жүктелуі қолжетімсіз.",
                "setup": setup,
            }
