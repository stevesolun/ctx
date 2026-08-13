"""Privacy-safe normalization of public current-work descriptions.

The normalizer deliberately produces only bounded canonical concepts.  It does
not retain source prose, paths, repository URLs, evaluator data, or file bodies.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from ctx.engine.planner import MAX_SIGNALS, WorkObservation


MAX_PUBLIC_WORK_SIGNALS = 8

LANGUAGE_ALIASES: dict[str, frozenset[str]] = {
    "c": frozenset({"c"}),
    "cpp": frozenset({"c++", "cplusplus", "cpp"}),
    "csharp": frozenset({"c#", "csharp", "dotnet"}),
    "go": frozenset({"go", "golang"}),
    "java": frozenset({"java"}),
    "javascript": frozenset({"javascript", "js", "node", "nodejs"}),
    "php": frozenset({"php"}),
    "python": frozenset({"py", "python"}),
    "rust": frozenset({"rs", "rust"}),
    "typescript": frozenset({"ts", "typescript"}),
}

_BACKTICK_RE = re.compile(r"`([^`\r\n]{1,256})`")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_ACRONYM_IDENTIFIER_RE = re.compile(r"[A-Z]{2,}[A-Z][a-z]")
_GENERIC_SIGNALS = frozenset(
    {
        "a",
        "add",
        "all",
        "an",
        "and",
        "args",
        "argument",
        "arguments",
        "as",
        "at",
        "avoid",
        "behavior",
        "better",
        "by",
        "catch",
        "change",
        "code",
        "default",
        "do",
        "emit",
        "error",
        "errors",
        "exact",
        "exactly",
        "existing",
        "false",
        "feature",
        "file",
        "focused",
        "for",
        "forward",
        "from",
        "helper",
        "if",
        "improve",
        "improvement",
        "implementation",
        "implement",
        "in",
        "including",
        "is",
        "it",
        "its",
        "keep",
        "key",
        "keys",
        "kwargs",
        "make",
        "must",
        "new",
        "newline",
        "no",
        "none",
        "not",
        "null",
        "object",
        "of",
        "on",
        "one",
        "only",
        "or",
        "parsed",
        "patch",
        "please",
        "preserve",
        "private",
        "reference",
        "raises",
        "repair",
        "repo",
        "repository",
        "requested",
        "return",
        "review",
        "safe",
        "safely",
        "same",
        "s",
        "sentinel",
        "small",
        "swallow",
        "test",
        "tests",
        "testing",
        "the",
        "this",
        "through",
        "trailing",
        "true",
        "unrelated",
        "update",
        "use",
        "valid",
        "value",
        "visible",
        "whatever",
        "when",
        "with",
        "write",
        "ensure",
        "ascii",
    }
)
_CANONICAL_FORMS = {
    "decode": "decoding",
    "decoded": "decoding",
    "decoder": "decoding",
    "decoders": "decoding",
    "decodes": "decoding",
    "serialize": "serialization",
    "serialized": "serialization",
    "serializes": "serialization",
    "serializing": "serialization",
    "exceptions": "exception",
}
_LANGUAGE_TOKENS = frozenset(
    token
    for canonical, aliases in LANGUAGE_ALIASES.items()
    for token in (canonical, *aliases)
    if token.isalnum()
)


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def _tokens(value: str, *, identifier: bool = False) -> tuple[str, ...]:
    normalized = _normalized_text(value)
    compact: tuple[str, ...] = ()
    if (
        identifier
        and re.fullmatch(r"[A-Za-z0-9]+", normalized) is not None
        and _ACRONYM_IDENTIFIER_RE.search(normalized)
    ):
        joined = "".join(_TOKEN_RE.findall(normalized.casefold()))
        if joined:
            compact = (joined[:128],)
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", normalized)
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", separated)
    values = tuple(token[:128] for token in _TOKEN_RE.findall(separated.casefold()))
    return compact + values


def _language(value: str) -> tuple[str, ...]:
    normalized = _normalized_text(value).strip().casefold()
    for canonical, aliases in LANGUAGE_ALIASES.items():
        if normalized == canonical or normalized in aliases:
            return (canonical,)
    return ()


def _specific(token: str) -> str | None:
    canonical = _CANONICAL_FORMS.get(token, token)
    if (
        not canonical
        or canonical in _GENERIC_SIGNALS
        or canonical in _LANGUAGE_TOKENS
        or len(canonical) > 128
    ):
        return None
    return canonical


def _repo_tokens(repo_slug: str) -> tuple[str, ...]:
    normalized = _normalized_text(repo_slug).strip().rstrip("/")
    if not normalized:
        return ()
    leaf = re.split(r"[/\\]", normalized)[-1]
    if leaf.casefold().endswith(".git"):
        leaf = leaf[:-4]
    return _tokens(leaf, identifier=True)


def normalize_public_current_work(
    *,
    query: str,
    task: str,
    language: str,
    repo_slug: str = "",
    repo_facts: Sequence[str] = (),
) -> WorkObservation:
    """Return a bounded observation derived only from public work metadata.

    Priority is repository identity, declared API identifiers, query concepts,
    task concepts, then sanitized repository facts.  Priority selection happens
    before canonical sorting so replay is deterministic without letting long
    task prose crowd out the strongest current-work anchors.
    """

    for name, value in (("query", query), ("task", task), ("language", language)):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
    if not isinstance(repo_slug, str):
        raise TypeError("repo_slug must be a string")
    if isinstance(repo_facts, (str, bytes, bytearray)) or not isinstance(repo_facts, Sequence):
        raise TypeError("repo_facts must be a bounded sequence of strings")
    if len(repo_facts) > MAX_SIGNALS or not all(isinstance(item, str) for item in repo_facts):
        raise ValueError("repo_facts must contain at most 32 strings")

    prioritized: list[str] = []
    seen: set[str] = set()

    def add(values: Sequence[str]) -> None:
        for value in values:
            token = _specific(value)
            if token is None or token in seen:
                continue
            prioritized.append(token)
            seen.add(token)
            if len(prioritized) >= MAX_PUBLIC_WORK_SIGNALS:
                return

    add(_repo_tokens(repo_slug))
    identifiers = tuple(
        token
        for text in (query, task)
        for identifier in _BACKTICK_RE.findall(text)
        for token in _tokens(identifier, identifier=True)
    )
    add(identifiers)
    add(_tokens(_BACKTICK_RE.sub(" ", query)))
    add(_tokens(_BACKTICK_RE.sub(" ", task)))
    add(tuple(token for fact in repo_facts for token in _tokens(fact, identifier=True)))

    signals = tuple(sorted(prioritized))
    languages = _language(language)
    return WorkObservation(
        signals=signals,
        languages=languages,
        requested_limit=5 if signals else 0,
    )


__all__ = [
    "LANGUAGE_ALIASES",
    "MAX_PUBLIC_WORK_SIGNALS",
    "normalize_public_current_work",
]
