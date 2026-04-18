#!/usr/bin/env python3
"""
embedding_backend.py -- Pluggable embedding backends for the intake gate.

Two backends ship in Phase 2:

  - SentenceTransformerEmbedder   default, zero external daemon.
                                  Uses sentence-transformers/all-MiniLM-L6-v2.
  - OllamaEmbedder                opt-in, requires a local ollama daemon with
                                  `nomic-embed-text` pulled.

The ``Embedder`` Protocol lets the rest of the intake gate stay
backend-agnostic. Both implementations return L2-normalised float32 vectors
so downstream cosine similarity is a single dot product.

Backend selection is centralised in ``get_embedder(name)``; callers pass the
string from ``ctx_config.intake.embedding.backend``. Heavy imports
(``sentence_transformers``, ``requests``) happen lazily inside the concrete
class to keep the module cheap to import.

Security:

  ``OllamaEmbedder`` only talks to a locally-bound host (localhost /
  127.0.0.1 / ::1) to prevent SSRF via a poisoned config pointing the
  embedder at internal metadata endpoints. Non-http(s) schemes are
  rejected. To reach a non-local ollama deployment, callers must opt in
  explicitly by passing ``allow_remote=True`` — this is a deliberate
  friction surface.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable
from urllib.parse import urlparse

import numpy as np


DEFAULT_ST_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_OLLAMA_MODEL = "nomic-embed-text"
DEFAULT_OLLAMA_URL = "http://localhost:11434"

# Output dim of nomic-embed-text at the version tested against. Kept as
# a named constant so a future upstream dim change only needs one edit.
_NOMIC_EMBED_TEXT_DIM = 768
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@runtime_checkable
class Embedder(Protocol):
    """One method: embed a batch of texts to a 2-D float32 matrix.

    ``dim`` and ``name`` are declared as ``@property`` so implementations are
    free to compute them lazily — important because models can be expensive
    to load and callers shouldn't pay that cost just to introspect metadata.
    """

    @property
    def dim(self) -> int: ...

    @property
    def name(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> np.ndarray: ...


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalisation with zero-vector safety."""
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.where(norms == 0.0, 1.0, norms)
    return (mat / norms).astype(np.float32, copy=False)


@dataclass
class SentenceTransformerEmbedder:
    """Local-only embedder; loads the model on first ``embed`` call."""

    model_name: str = DEFAULT_ST_MODEL
    # ``init=False`` keeps the lazy model out of the generated __init__
    # so callers can't inject a fake model via the constructor.
    _model: Any = field(init=False, default=None, repr=False, compare=False)

    @property
    def name(self) -> str:
        return f"sentence-transformers:{self.model_name}"

    @property
    def dim(self) -> int:
        # Returns -1 until the model has been loaded (first ``embed`` call).
        # This keeps the attribute cheap to touch — important for
        # ``runtime_checkable`` ``Protocol`` ``isinstance`` checks, which call
        # ``hasattr`` on every declared attribute.
        if self._model is None:
            return -1
        return int(self._model.get_sentence_embedding_dimension())

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is not installed; "
                "pip install sentence-transformers or switch intake.embedding.backend "
                "to 'ollama'"
            ) from exc
        self._model = SentenceTransformer(self.model_name)

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        self._ensure_loaded()
        vecs = self._model.encode(
            list(texts),
            convert_to_numpy=True,
            normalize_embeddings=False,
            show_progress_bar=False,
        )
        return _l2_normalize(np.asarray(vecs, dtype=np.float32))


class OllamaEmbedderError(RuntimeError):
    """Raised when an Ollama request fails. Carries the failing text index."""

    def __init__(self, index: int, message: str) -> None:
        super().__init__(f"[text #{index}] {message}")
        self.index = index


@dataclass
class OllamaEmbedder:
    """HTTP-backed embedder for a local ollama daemon. Opt-in.

    Partial failures fail fast: if text *k* of *N* errors, an
    ``OllamaEmbedderError`` is raised with ``.index == k`` and no
    partial batch is returned. Callers who want best-effort behaviour
    must wrap single-text calls themselves.
    """

    model_name: str = DEFAULT_OLLAMA_MODEL
    base_url: str = DEFAULT_OLLAMA_URL
    timeout: float = 30.0
    allow_remote: bool = False

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"base_url scheme must be http or https: {self.base_url!r}"
            )
        host = (parsed.hostname or "").lower()
        if not host:
            raise ValueError(f"base_url has no host: {self.base_url!r}")
        if not self.allow_remote and host not in _LOCAL_HOSTS:
            raise ValueError(
                f"base_url host {host!r} is not local; pass allow_remote=True "
                f"to explicitly allow a remote ollama instance"
            )

    @property
    def name(self) -> str:
        return f"ollama:{self.model_name}"

    @property
    def dim(self) -> int:
        # Only known after the first successful call; nomic-embed-text's
        # output dim is stable at the version we test against. Callers
        # that need dim up front should do a one-token warmup embed.
        return _NOMIC_EMBED_TEXT_DIM if self.model_name == DEFAULT_OLLAMA_MODEL else -1

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        try:
            import requests  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "requests is required for the ollama backend; pip install requests"
            ) from exc

        url = f"{self.base_url.rstrip('/')}/api/embeddings"
        rows: list[list[float]] = []
        for idx, text in enumerate(texts):
            try:
                resp = requests.post(
                    url,
                    json={"model": self.model_name, "prompt": text},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                raise OllamaEmbedderError(idx, str(exc)) from exc
            if "embedding" not in payload:
                raise OllamaEmbedderError(
                    idx, f"response missing 'embedding' key: {payload!r}"
                )
            rows.append(payload["embedding"])
        return _l2_normalize(np.asarray(rows, dtype=np.float32))


def get_embedder(
    backend: str = "sentence-transformers",
    *,
    model: str | None = None,
    base_url: str | None = None,
    allow_remote: bool = False,
) -> Embedder:
    """Factory: map a backend name to a concrete ``Embedder``.

    The default is ``sentence-transformers`` (no external daemon required).
    ``ollama`` is opt-in and requires the user to have ollama running with the
    chosen model pulled. ``allow_remote`` must be set explicitly to reach a
    non-local ollama host.
    """
    key = (backend or "").strip().lower()
    if key in ("", "sentence-transformers", "st", "sbert"):
        return SentenceTransformerEmbedder(model_name=model or DEFAULT_ST_MODEL)
    if key in ("ollama", "ol"):
        return OllamaEmbedder(
            model_name=model or DEFAULT_OLLAMA_MODEL,
            base_url=base_url or os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL),
            allow_remote=allow_remote,
        )
    raise ValueError(
        f"unknown embedding backend {backend!r}; expected "
        f"'sentence-transformers' or 'ollama'"
    )
