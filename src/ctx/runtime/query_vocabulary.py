"""Authenticated, catalog-bound vocabulary for query-only work normalization.

This value object carries no catalog-loading policy.  A trusted catalog factory
must construct it from the exact catalog it opens, and the query facade must
compare both catalog bindings before normalizing host text.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from ctx.engine.observation import LANGUAGE_ALIASES


QUERY_VOCABULARY_SCHEMA: Final = "ctx.authenticated-query-vocabulary-v1"
MAX_QUERY_SIGNAL_VOCABULARY: Final = 4096

_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_SAFE_SIGNAL_RE = re.compile(r"\A[a-z0-9][a-z0-9._:@-]{0,127}\Z")
_WORD_RE = re.compile(r"[a-z0-9]+")


def _signal_words(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value)
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", normalized)
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", separated)
    return tuple(_WORD_RE.findall(separated.casefold()))


_LANGUAGE_WORD_KEYS = frozenset(
    _signal_words(token)
    for canonical, aliases in LANGUAGE_ALIASES.items()
    for token in (canonical, *aliases)
)
_LANGUAGE_COMPACT_KEYS = frozenset("".join(words) for words in _LANGUAGE_WORD_KEYS)


def _digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _vocabulary_digest(
    *,
    signals: Sequence[str],
    catalog_namespace_digest: str,
    graph_artifact_sha256: str,
) -> str:
    payload = {
        "catalog_namespace_digest": catalog_namespace_digest,
        "graph_artifact_sha256": graph_artifact_sha256,
        "schema": QUERY_VOCABULARY_SCHEMA,
        "signals": list(signals),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_lexical_contract(signals: Sequence[str]) -> None:
    word_owners: dict[tuple[str, ...], str] = {}
    compact_owners: dict[str, str] = {}
    for signal in signals:
        words = _signal_words(signal)
        compact = "".join(words)
        if words in _LANGUAGE_WORD_KEYS or compact in _LANGUAGE_COMPACT_KEYS:
            raise ValueError("language aliases cannot enter query vocabulary")
        prior = word_owners.get(words)
        compact_prior = compact_owners.get(compact)
        if (prior is not None and prior != signal) or (
            compact_prior is not None and compact_prior != signal
        ):
            raise ValueError("signals contain a lexical collision")
        word_owners[words] = signal
        compact_owners[compact] = signal


@dataclass(frozen=True, slots=True)
class AuthenticatedQueryVocabulary:
    """Privacy-approved signals bound to one exact catalog artifact."""

    signals: tuple[str, ...]
    catalog_namespace_digest: str
    graph_artifact_sha256: str
    vocabulary_digest: str

    def __post_init__(self) -> None:
        _digest(self.catalog_namespace_digest, "catalog_namespace_digest")
        _digest(self.graph_artifact_sha256, "graph_artifact_sha256")
        _digest(self.vocabulary_digest, "vocabulary_digest")
        if not isinstance(self.signals, tuple):
            raise TypeError("signals must be an immutable tuple")
        if len(self.signals) > MAX_QUERY_SIGNAL_VOCABULARY:
            raise ValueError("signals exceed the authenticated vocabulary bound")
        if tuple(sorted(set(self.signals))) != self.signals:
            raise ValueError("signals must be sorted and unique")
        if any(_SAFE_SIGNAL_RE.fullmatch(signal) is None for signal in self.signals):
            raise ValueError("signals must contain only canonical safe tokens")
        _validate_lexical_contract(self.signals)
        if self.vocabulary_digest != _vocabulary_digest(
            signals=self.signals,
            catalog_namespace_digest=self.catalog_namespace_digest,
            graph_artifact_sha256=self.graph_artifact_sha256,
        ):
            raise ValueError("vocabulary digest does not match its catalog bindings")

    @classmethod
    def create(
        cls,
        *,
        signals: Sequence[str],
        catalog_namespace_digest: str,
        graph_artifact_sha256: str,
    ) -> AuthenticatedQueryVocabulary:
        """Construct a canonical value for a trusted catalog factory."""

        if isinstance(signals, (str, bytes, bytearray)) or not isinstance(signals, Sequence):
            raise TypeError("signals must be a bounded sequence of strings")
        if not all(isinstance(signal, str) for signal in signals):
            raise TypeError("signals must contain only strings")
        ordered = tuple(sorted(signals))
        return cls(
            signals=ordered,
            catalog_namespace_digest=catalog_namespace_digest,
            graph_artifact_sha256=graph_artifact_sha256,
            vocabulary_digest=_vocabulary_digest(
                signals=ordered,
                catalog_namespace_digest=catalog_namespace_digest,
                graph_artifact_sha256=graph_artifact_sha256,
            ),
        )


__all__ = [
    "MAX_QUERY_SIGNAL_VOCABULARY",
    "QUERY_VOCABULARY_SCHEMA",
    "AuthenticatedQueryVocabulary",
]
