"""
test_embedding_backend.py -- Tests for the Phase 2 embedding abstraction.

Network and model downloads are avoided: both backends are stubbed at the
import boundary so tests run in any CI environment.
"""

from __future__ import annotations

import sys
from dataclasses import fields
from pathlib import Path
from typing import Any, Sequence
from unittest.mock import MagicMock

import numpy as np
import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import embedding_backend as eb  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Factory
# ────────────────────────────────────────────────────────────────────


def test_factory_defaults_to_sentence_transformers() -> None:
    e = eb.get_embedder()
    assert isinstance(e, eb.SentenceTransformerEmbedder)
    assert e.name.startswith("sentence-transformers:")


@pytest.mark.parametrize("alias", ["sentence-transformers", "st", "sbert", "SBERT", ""])
def test_factory_accepts_st_aliases(alias: str) -> None:
    assert isinstance(eb.get_embedder(alias), eb.SentenceTransformerEmbedder)


@pytest.mark.parametrize("alias", ["ollama", "Ollama", "OL"])
def test_factory_accepts_ollama_aliases(alias: str) -> None:
    assert isinstance(eb.get_embedder(alias), eb.OllamaEmbedder)


def test_factory_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown embedding backend"):
        eb.get_embedder("cohere")


def test_factory_applies_custom_model_on_ollama() -> None:
    e = eb.get_embedder(
        "ollama", model="mxbai-embed-large", base_url="http://localhost:11434"
    )
    assert isinstance(e, eb.OllamaEmbedder)
    assert e.model_name == "mxbai-embed-large"


def test_factory_ollama_honours_env_url_when_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9999")
    e = eb.get_embedder("ollama")
    assert isinstance(e, eb.OllamaEmbedder)
    assert e.base_url == "http://127.0.0.1:9999"


def test_factory_ollama_env_url_non_local_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_URL", "http://169.254.169.254")
    with pytest.raises(ValueError, match="not local"):
        eb.get_embedder("ollama")


def test_factory_ollama_allow_remote_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_URL", "http://my-remote-host:11434")
    e = eb.get_embedder("ollama", allow_remote=True)
    assert isinstance(e, eb.OllamaEmbedder)
    assert e.base_url == "http://my-remote-host:11434"


# ────────────────────────────────────────────────────────────────────
# _l2_normalize
# ────────────────────────────────────────────────────────────────────


def test_l2_normalize_produces_unit_rows() -> None:
    m = np.array([[3.0, 4.0], [1.0, 0.0], [0.0, 0.0]], dtype=np.float32)
    out = eb._l2_normalize(m)
    assert out.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(out[0]), 1.0, atol=1e-6)
    np.testing.assert_allclose(np.linalg.norm(out[1]), 1.0, atol=1e-6)
    np.testing.assert_array_equal(out[2], np.zeros(2, dtype=np.float32))


# ────────────────────────────────────────────────────────────────────
# SentenceTransformerEmbedder
# ────────────────────────────────────────────────────────────────────


class _FakeSTModel:
    """Minimal stand-in for sentence_transformers.SentenceTransformer."""

    def __init__(self, dim: int = 8) -> None:
        self._dim = dim

    def get_sentence_embedding_dimension(self) -> int:
        return self._dim

    def encode(self, texts: Sequence[str], **_: Any) -> np.ndarray:
        rows = []
        for t in texts:
            h = abs(hash(t))
            rng = np.random.default_rng(h % (2**32))
            rows.append(rng.normal(size=self._dim))
        return np.asarray(rows, dtype=np.float32)


def _install_fake_st(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_module = MagicMock()
    fake_module.SentenceTransformer = lambda model_name: _FakeSTModel()
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)


def test_st_embedder_empty_input_returns_empty_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_st(monkeypatch)
    e = eb.SentenceTransformerEmbedder()
    out = e.embed([])
    assert out.shape == (0, 0)
    assert out.dtype == np.float32


def test_st_embedder_returns_normalised_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_st(monkeypatch)
    e = eb.SentenceTransformerEmbedder()
    out = e.embed(["hello", "world"])
    assert out.shape == (2, 8)
    norms = np.linalg.norm(out, axis=1)
    np.testing.assert_allclose(norms, np.ones(2), atol=1e-6)


def test_st_embedder_dim_is_minus_one_before_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_st(monkeypatch)
    e = eb.SentenceTransformerEmbedder()
    assert e.dim == -1
    e.embed(["warmup"])
    assert e.dim == 8


def test_st_embedder_missing_package_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    e = eb.SentenceTransformerEmbedder()
    with pytest.raises(RuntimeError, match="sentence-transformers is not installed"):
        e.embed(["hi"])


def test_st_embedder_model_field_is_not_in_init() -> None:
    # ``_model`` must not be injectable via the constructor — prevents
    # callers from slipping in a fake implementation.
    init_fields = {f.name for f in fields(eb.SentenceTransformerEmbedder) if f.init}
    assert "_model" not in init_fields
    # The field still exists on instances (default None).
    assert eb.SentenceTransformerEmbedder()._model is None


# ────────────────────────────────────────────────────────────────────
# OllamaEmbedder — SSRF guard
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://169.254.169.254",      # AWS IMDS
        "http://metadata.google.internal",
        "http://10.0.0.5",
        "http://internal-service.corp",
    ],
)
def test_ollama_rejects_non_local_host_by_default(bad_url: str) -> None:
    with pytest.raises(ValueError, match="not local"):
        eb.OllamaEmbedder(base_url=bad_url)


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "ftp://localhost:11434",
        "gopher://localhost",
        "",
        "not-a-url",
    ],
)
def test_ollama_rejects_bad_scheme_or_missing_host(bad_url: str) -> None:
    with pytest.raises(ValueError):
        eb.OllamaEmbedder(base_url=bad_url)


@pytest.mark.parametrize(
    "good_url",
    [
        "http://localhost:11434",
        "http://127.0.0.1",
        "https://localhost",
        "http://[::1]:11434",
    ],
)
def test_ollama_accepts_local_hosts(good_url: str) -> None:
    e = eb.OllamaEmbedder(base_url=good_url)
    assert e.base_url == good_url


def test_ollama_allow_remote_opt_in() -> None:
    e = eb.OllamaEmbedder(base_url="http://my-gpu-box:11434", allow_remote=True)
    assert e.allow_remote is True


# ────────────────────────────────────────────────────────────────────
# OllamaEmbedder — embed()
# ────────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


def _install_fake_requests(
    monkeypatch: pytest.MonkeyPatch,
    vectors: list[list[float]],
    *,
    missing_key: bool = False,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    queue = list(vectors)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> _FakeResponse:
        calls.append({"url": url, "json": json, "timeout": timeout})
        if missing_key:
            return _FakeResponse({"not_embedding": queue.pop(0)})
        return _FakeResponse({"embedding": queue.pop(0)})

    fake_module = MagicMock()
    fake_module.post = fake_post
    monkeypatch.setitem(sys.modules, "requests", fake_module)
    return calls


def test_ollama_embedder_empty_input_returns_empty() -> None:
    out = eb.OllamaEmbedder().embed([])
    assert out.shape == (0, 0)


def test_ollama_embedder_posts_per_text_and_normalises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_requests(
        monkeypatch,
        vectors=[[3.0, 4.0], [1.0, 0.0]],
    )
    e = eb.OllamaEmbedder(base_url="http://localhost:11434")
    out = e.embed(["a", "b"])

    assert len(calls) == 2
    assert calls[0]["url"] == "http://localhost:11434/api/embeddings"
    assert calls[0]["json"] == {"model": eb.DEFAULT_OLLAMA_MODEL, "prompt": "a"}
    np.testing.assert_allclose(np.linalg.norm(out, axis=1), np.ones(2), atol=1e-6)
    assert out.dtype == np.float32


def test_ollama_embedder_missing_key_raises_with_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_requests(monkeypatch, vectors=[[1.0]], missing_key=True)
    with pytest.raises(eb.OllamaEmbedderError, match="text #0") as exc_info:
        eb.OllamaEmbedder().embed(["x"])
    assert exc_info.value.index == 0
    assert "missing 'embedding' key" in str(exc_info.value)


def test_ollama_embedder_partial_failure_reports_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # First two succeed, third raises — error must carry index=2.
    fake_module = MagicMock()
    call_count = {"n": 0}

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> _FakeResponse:
        call_count["n"] += 1
        if call_count["n"] <= 2:
            return _FakeResponse({"embedding": [float(call_count["n"])]})
        raise RuntimeError("boom")

    fake_module.post = fake_post
    monkeypatch.setitem(sys.modules, "requests", fake_module)

    with pytest.raises(eb.OllamaEmbedderError, match="text #2") as exc_info:
        eb.OllamaEmbedder().embed(["a", "b", "c"])
    assert exc_info.value.index == 2


def test_ollama_embedder_missing_requests_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "requests", None)
    with pytest.raises(RuntimeError, match="requests is required"):
        eb.OllamaEmbedder().embed(["x"])


def test_ollama_embedder_reports_known_dim_for_nomic() -> None:
    assert eb.OllamaEmbedder().dim == eb._NOMIC_EMBED_TEXT_DIM
    assert eb.OllamaEmbedder(model_name="other").dim == -1


# ────────────────────────────────────────────────────────────────────
# Protocol conformance
# ────────────────────────────────────────────────────────────────────


def test_both_backends_conform_to_protocol() -> None:
    assert isinstance(eb.SentenceTransformerEmbedder(), eb.Embedder)
    assert isinstance(eb.OllamaEmbedder(), eb.Embedder)
