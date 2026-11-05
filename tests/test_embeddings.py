import sys
from types import ModuleType

from app.embeddings import LocalEmbedder


def test_local_embedding_load_is_offline_cpu_and_uses_configured_prefix(
    settings, tmp_path, monkeypatch
):
    model_dir = tmp_path / "local-embedding"
    model_dir.mkdir()
    settings = settings.model_copy(
        update={"embedding_model": str(model_dir), "embedding_prefix": "query: "}
    )
    loaded = []
    encoded = []

    class TestModel:
        def encode(self, texts, **kwargs):
            encoded.append((list(texts), kwargs))
            return [[1.0, 0.0] for _ in texts]

    def model_factory(path, **kwargs):
        loaded.append((path, kwargs))
        return TestModel()

    test_module = ModuleType("sentence_transformers")
    test_module.SentenceTransformer = model_factory
    monkeypatch.setitem(sys.modules, "sentence_transformers", test_module)
    embedder = LocalEmbedder(settings)
    assert embedder.encode(["Құпиясөзді енгізу қатесі."]) == [[1.0, 0.0]]
    assert embedder.encode(["Пароль не соответствует правилам."]) == [[1.0, 0.0]]
    assert len(loaded) == 1
    assert loaded[0] == (
        str(model_dir.resolve()),
        {"device": "cpu", "local_files_only": True, "trust_remote_code": False, "token": False},
    )
    assert encoded[0][0] == ["Құпиясөзді енгізу қатесі."]
    assert encoded[1][0] == ["Пароль не соответствует правилам."]
    assert all(options["prompt"] == "query: " for _, options in encoded)
    assert all(options["normalize_embeddings"] is True for _, options in encoded)
