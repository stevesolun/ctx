"""Privacy-safe current-work normalization for query-only planning.

Raw task text is inspected only in memory.  Emitted concepts must come from an
exact catalog-bound :class:`AuthenticatedQueryVocabulary`; paths, inline
secrets, language aliases, unknown identifiers, and source prose cannot cross
the authoritative engine boundary.
"""

from __future__ import annotations

import re
import unicodedata

from ctx.engine.observation import MAX_PUBLIC_WORK_SIGNALS, normalize_public_current_work
from ctx.engine.planner import WorkObservation
from ctx.runtime.query_vocabulary import AuthenticatedQueryVocabulary
from ctx.utils._secret_scan import SECRET_ASSIGNMENT_RE, redact_secret_text


MAX_QUERY_TASK_BYTES = 64 * 1024
MAX_QUERY_TASK_CODEPOINTS = 64 * 1024
MAX_QUERY_LANGUAGE_BYTES = 128
MAX_QUERY_LANGUAGE_CODEPOINTS = 128
MAX_QUERY_WORDS = 4096
MAX_QUERY_SIGNAL_WORDS = 16

_LEXEME_RE = re.compile(r"[A-Za-z0-9]+")
_WORD_RE = re.compile(r"[a-z0-9]+")
_URL_RE = re.compile(r"(?i)\b(?:https?|file)://[^\s`'\"<>]+")
_WRAPPED_SPAN_RE = re.compile(r"`[^`\r\n]*`|<[^<>\r\n]*>")
_WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\)[^\s`'\"<>]+")
_POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:~?/|\.{1,2}/)[^\s`'\"<>]+")
_QUOTED_PATH_RE = re.compile(r"([\"'])(?=[^\"'\r\n]*[\\/])[^\"'\r\n]*\1")
_RELATIVE_PATH_RE = re.compile(r"(?<!\S)[^\s`'\"<>]*[\\/][^\s`'\"<>]*")
_AUTHORIZATION_SCHEME_RE = re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:bearer|basic)\s+[^\s,;}]+")
_JSON_SECRET_RE = re.compile(
    r"(?i)(?:(?:--)?\"?(?:token|secret|password|passwd|api[_-]?key|private[_-]?key|"
    r"credential|access[_-]?key|refresh[_-]?token|client[_-]?secret|authorization|bearer)\"?"
    r"(?:\s*[:=]\s*|\s+is\s*:?[ \t]*|\s+))(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)


class _TrieNode:
    __slots__ = ("children", "signal")

    def __init__(self) -> None:
        self.children: dict[str, _TrieNode] = {}
        self.signal: str | None = None


def _strip_or_unwrap_spans(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        content = match.group(0)[1:-1]
        return " " if "/" in content or "\\" in content else f" {content} "

    return _WRAPPED_SPAN_RE.sub(replace, value)


def _bounded_task(value: str) -> str:
    if len(value) > MAX_QUERY_TASK_CODEPOINTS:
        raise ValueError("task exceeds the query normalization bound")
    try:
        encoded_size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("task must contain valid Unicode scalar values") from exc
    if encoded_size > MAX_QUERY_TASK_BYTES:
        raise ValueError("task exceeds the query normalization bound")
    unwrapped = _strip_or_unwrap_spans(value)
    without_formatting = (
        unwrapped.replace("**", "").replace("__", "").replace("~~", "").replace("*", "")
    )
    without_paths = _RELATIVE_PATH_RE.sub(
        " ",
        _WINDOWS_PATH_RE.sub(
            " ",
            _POSIX_PATH_RE.sub(
                " ",
                _QUOTED_PATH_RE.sub(" ", _URL_RE.sub(" ", without_formatting)),
            ),
        ),
    )
    without_authorization = _AUTHORIZATION_SCHEME_RE.sub(" ", without_paths)
    without_secrets = _JSON_SECRET_RE.sub(" ", without_authorization)
    without_assignments = SECRET_ASSIGNMENT_RE.sub(" ", without_secrets)
    return redact_secret_text(without_assignments)


def _bounded_language(value: str) -> str:
    if len(value) > MAX_QUERY_LANGUAGE_CODEPOINTS:
        raise ValueError("language exceeds the query normalization bound")
    try:
        encoded_size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("language must contain valid Unicode scalar values") from exc
    if encoded_size > MAX_QUERY_LANGUAGE_BYTES:
        raise ValueError("language exceeds the query normalization bound")
    return value


def _split_camel(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value)
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", normalized)
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", separated)
    return tuple(_WORD_RE.findall(separated.casefold()))


def _task_streams(value: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    compact: list[str] = []
    segmented: list[str] = []
    for lexeme in _LEXEME_RE.findall(unicodedata.normalize("NFKC", value)):
        compact_value = "".join(_WORD_RE.findall(lexeme.casefold()))
        if compact_value:
            compact.append(compact_value)
        segmented.extend(_split_camel(lexeme))
        if len(compact) > MAX_QUERY_WORDS or len(segmented) > MAX_QUERY_WORDS:
            raise ValueError("task exceeds the normalized word bound")
    return tuple(compact), tuple(segmented)


def _build_trie(vocabulary: AuthenticatedQueryVocabulary) -> _TrieNode:
    root = _TrieNode()
    lexical_forms: dict[tuple[str, ...], str] = {}
    compact_forms: dict[str, str] = {}
    for signal in vocabulary.signals:
        words = tuple(_WORD_RE.findall(signal))
        if not words or len(words) > MAX_QUERY_SIGNAL_WORDS:
            raise ValueError("authenticated query signal exceeds the lexical bound")
        prior = lexical_forms.get(words)
        if prior is not None and prior != signal:
            raise ValueError("authenticated query vocabulary is lexically ambiguous")
        compact = "".join(words)
        compact_prior = compact_forms.get(compact)
        if compact_prior is not None and compact_prior != signal:
            raise ValueError("authenticated query vocabulary is lexically ambiguous")
        lexical_forms[words] = signal
        compact_forms[compact] = signal
        node = root
        for word in words:
            node = node.children.setdefault(word, _TrieNode())
        node.signal = signal
    return root


def _matches(stream: tuple[str, ...], trie: _TrieNode) -> dict[str, int]:
    found: dict[str, int] = {}
    for start in range(len(stream)):
        node = trie
        for word in stream[start : start + MAX_QUERY_SIGNAL_WORDS]:
            child = node.children.get(word)
            if child is None:
                break
            node = child
            if node.signal is not None:
                found.setdefault(node.signal, start)
    return found


def normalize_query_work(
    *,
    task: str,
    language: str,
    vocabulary: AuthenticatedQueryVocabulary,
    expected_catalog_namespace_digest: str,
    expected_graph_artifact_sha256: str,
) -> WorkObservation:
    """Return a bounded observation from one exact catalog vocabulary."""

    if not isinstance(task, str):
        raise TypeError("task must be a string")
    if not isinstance(language, str):
        raise TypeError("language must be a string")
    if not isinstance(vocabulary, AuthenticatedQueryVocabulary):
        raise TypeError("vocabulary must be an AuthenticatedQueryVocabulary")
    if (
        vocabulary.catalog_namespace_digest != expected_catalog_namespace_digest
        or vocabulary.graph_artifact_sha256 != expected_graph_artifact_sha256
    ):
        raise ValueError("authenticated query vocabulary has a catalog binding mismatch")

    compact, segmented = _task_streams(_bounded_task(task))
    trie = _build_trie(vocabulary)
    positions = _matches(compact, trie)
    for signal, position in _matches(segmented, trie).items():
        positions[signal] = min(position, positions.get(signal, position))
    selected = tuple(
        sorted(
            signal
            for signal, _ in sorted(
                positions.items(),
                key=lambda item: (item[1], item[0]),
            )[:MAX_PUBLIC_WORK_SIGNALS]
        )
    )
    languages = normalize_public_current_work(
        query="",
        task="",
        language=_bounded_language(language),
    ).languages
    return WorkObservation(
        signals=selected,
        languages=languages,
        requested_limit=5 if selected else 0,
    )


__all__ = [
    "MAX_QUERY_TASK_BYTES",
    "MAX_QUERY_TASK_CODEPOINTS",
    "MAX_QUERY_LANGUAGE_BYTES",
    "MAX_QUERY_LANGUAGE_CODEPOINTS",
    "MAX_QUERY_WORDS",
    "normalize_query_work",
]
