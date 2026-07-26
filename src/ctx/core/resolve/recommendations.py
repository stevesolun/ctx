"""Shared recommendation ranking for ctx graph-backed surfaces."""

from __future__ import annotations

import functools
import json
import math
import re
import sqlite3
import weakref
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any


_INDEXED_RECOMMENDATION_GRAPH_MARKER = object()
_MAX_RECOMMENDATION_SIGNALS = 64
_TAG_STOPWORDS: frozenset[str] = frozenset(
    {
        # Tiny stoplist for query-to-tags tokenisation. This is intentionally
        # lightweight; callers needing precision can pass explicit graph seeds.
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "for",
        "with",
        "of",
        "to",
        "on",
        "in",
        "at",
        "by",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "how",
        "what",
        "when",
        "where",
        "why",
        "which",
        "who",
        "can",
        "i",
        "you",
        "me",
        "my",
        "your",
        "our",
        "we",
        "they",
        "their",
        "help",
        "please",
        "need",
        "want",
        "use",
        "using",
        "find",
        "looking",
        "looking-for",
        "task",
    }
)


_SLUG_TOKEN_RE = re.compile(r"[-_/]+")


def _slug_tokens(label: str) -> set[str]:
    """Split a slug-shaped label into exact tokens.

    Used at query time to distinguish exact slug-token matches from
    bare substring matches. ``python-fastapi-development`` becomes
    ``{python, fastapi, development}`` — so a query for ``fastapi`` is
    a clean slug-token hit, while a query for ``api`` is *only* a
    substring hit (and worth less in the rank).
    """
    return {tok for tok in _SLUG_TOKEN_RE.split(label.lower()) if tok}


def _truthy_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


# Cache token IDF per live graph object. The graph object is built once
# per process by the recommender entry points; recomputing the IDF on
# every query would be wasteful (one pass over 100K+ labels). A weak-key
# cache avoids stale hits when CPython reuses an object id after a graph
# fixture is collected.
_idf_cache: weakref.WeakKeyDictionary[Any, dict[str, float]] = weakref.WeakKeyDictionary()


# Cache the (vec_matrix, node_ids, model_id) tuple per live graph object.
# Loading the .npz + topk-state is ~50ms on a 13K-node graph; we don't
# want to pay it on every recommend_by_tags call. Weak keys keep the cache
# tied to the actual graph lifetime instead of a reusable object id.
_semantic_cache: weakref.WeakKeyDictionary[Any, tuple[Any, tuple[str, ...], str] | None] = (
    weakref.WeakKeyDictionary()
)


def _load_semantic_index(
    graph: Any,
    cache_dir: Path | None,
) -> tuple[Any, tuple[str, ...], str] | None:
    """Build a node-id → vector index from the graphify embedding cache.

    Returns ``(vecs_matrix, ordered_node_ids, model_id)`` where
    ``vecs_matrix[i]`` is the L2-normalised embedding for
    ``ordered_node_ids[i]``. Returns ``None`` if the cache is missing or
    the cache's ``embeddings.npz`` and ``topk-state.json`` are out of
    sync — a stale cache silently degrades semantic ranking, but tag +
    token + degree ranking still work.
    """
    if graph in _semantic_cache:
        return _semantic_cache[graph]

    if cache_dir is None:
        try:
            from ctx_config import cfg  # noqa: PLC0415

            cache_dir = cfg.graph_semantic_cache_dir
        except Exception:
            _semantic_cache[graph] = None
            return None

    npz = cache_dir / "embeddings.npz"
    state = cache_dir / "topk-state.json"
    if not (npz.is_file() and state.is_file()):
        _semantic_cache[graph] = None
        return None
    try:
        import numpy as np  # noqa: PLC0415

        data = np.load(npz, allow_pickle=False)
        hashes = [h.decode("utf-8") if isinstance(h, bytes) else str(h) for h in data["hashes"]]
        vecs = data["vecs"]
        model_arr = data["model"] if "model" in data.files else None
        model_id = ""
        if model_arr is not None and model_arr.size:
            model_id = str(model_arr.item()) if model_arr.ndim == 0 else str(model_arr[0])
        state_raw = json.loads(state.read_text(encoding="utf-8"))
        nodes_map = state_raw.get("nodes", {})
        if not isinstance(nodes_map, dict):
            raise ValueError("malformed topk-state")
        hash_to_idx = {h: i for i, h in enumerate(hashes)}

        ordered_ids: list[str] = []
        ordered_vecs: list = []
        for nid in graph.nodes:
            entry = nodes_map.get(nid)
            if not isinstance(entry, dict):
                continue
            ch = entry.get("content_hash")
            if not isinstance(ch, str):
                continue
            idx = hash_to_idx.get(ch)
            if idx is None:
                continue
            ordered_ids.append(nid)
            ordered_vecs.append(vecs[idx])
        if not ordered_ids:
            _semantic_cache[graph] = None
            return None
        mat = np.asarray(ordered_vecs, dtype="float32")
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        mat = mat / norms
        result = (mat, tuple(ordered_ids), model_id)
        _semantic_cache[graph] = result
        return result
    except Exception:
        # Any exception path (numpy missing, file unreadable, malformed
        # state, etc.) silently disables semantic boost. The non-semantic
        # ranking still works.
        _semantic_cache[graph] = None
        return None


def _embed_query(query: str, model_id: str) -> Any | None:
    """Embed a free-text query with the same model graphify uses.

    Returns an L2-normalised float32 vector (dim 384 for MiniLM-L6),
    or ``None`` if the embedder isn't available. Callers must tolerate
    None and fall through to non-semantic ranking.
    """
    if not model_id:
        return None
    try:
        # ``model_id`` is encoded as ``<backend>:<model_name>``.
        if ":" in model_id:
            backend, _, model_name = model_id.partition(":")
        else:
            backend, model_name = "sentence-transformers", model_id
        import numpy as np  # noqa: PLC0415

        if backend in {"sentence-transformers", "st", "sbert"}:
            from sentence_transformers import (  # type: ignore[import-not-found]  # noqa: PLC0415
                SentenceTransformer,
            )

            model = SentenceTransformer(model_name, local_files_only=True)
            v = model.encode(
                [query],
                convert_to_numpy=True,
                normalize_embeddings=False,
                show_progress_bar=False,
            )[0]
        else:
            from embedding_backend import get_embedder  # noqa: PLC0415

            embedder = get_embedder(backend, model=model_name)
            v = embedder.embed([query])[0]
        v = np.asarray(v, dtype="float32")
        n = float(np.linalg.norm(v))
        if n == 0:
            return None
        return v / n
    except Exception:
        return None


def _token_idf(graph: Any) -> dict[str, float]:
    """Inverse-document-frequency table over slug tokens in ``graph``.

    A token's IDF is ``log(N / df)`` where ``df`` is the number of
    nodes whose label or tags contain that token (after slug-tokenisation).
    Common tokens (``python`` over ~600 nodes) get IDF near 0; rare
    tokens (``fastapi`` over ~10 nodes) get IDF around 7. The query
    ranker multiplies match scores by IDF so rare tokens dominate.
    """
    graph_metadata = getattr(graph, "graph", {})
    indexed = graph_metadata.get("_ctx_recommendation_token_idf")
    if graph_metadata.get(
        "_ctx_recommendation_graph_marker"
    ) is _INDEXED_RECOMMENDATION_GRAPH_MARKER and isinstance(indexed, dict):
        return {str(token): float(value) for token, value in indexed.items()}

    cached = _idf_cache.get(graph)
    if cached is not None:
        return cached
    df: dict[str, int] = defaultdict(int)
    n = 0
    for node_id, data in graph.nodes(data=True):
        if data.get("external") or data.get("type") == "external-skill":
            continue
        label = str(data.get("label") or _node_name(node_id))
        n += 1
        tokens = set(_slug_tokens(label))
        for tag in data.get("tags", []):
            tokens.update(_slug_tokens(str(tag)))
        for tok in tokens:
            if len(tok) >= 3:
                df[tok] += 1
    if n == 0:
        return {}
    table = {tok: math.log(n / max(d, 1)) for tok, d in df.items()}
    _idf_cache[graph] = table
    return table


def _recommendation_degree(graph: Any, node_id: Any, node_data: dict[str, Any]) -> float:
    indexed = node_data.get("_ctx_recommendation_degree")
    if (
        getattr(graph, "graph", {}).get("_ctx_recommendation_graph_marker")
        is _INDEXED_RECOMMENDATION_GRAPH_MARKER
        and isinstance(indexed, int | float)
        and indexed >= 0
    ):
        return float(indexed)
    return float(graph.degree(node_id))


def query_to_tags(query: str) -> list[str]:
    """Extract tag-shaped signals from a free-text query."""
    tokens = re.findall(r"[A-Za-z0-9_/\-]+", query.lower())
    seen: dict[str, None] = {}
    for raw_token in tokens:
        if "/" not in raw_token and _SLUG_TOKEN_RE.search(raw_token):
            if len(raw_token) >= 3 and raw_token not in _TAG_STOPWORDS:
                seen.setdefault(raw_token, None)
        for token in _slug_token_parts(raw_token):
            if len(token) < 3 or token in _TAG_STOPWORDS:
                continue
            seen.setdefault(token, None)
    return _normalise_signals(list(seen.keys()))


def _slug_token_parts(label: str) -> list[str]:
    return [tok for tok in _SLUG_TOKEN_RE.split(label.lower()) if tok]


def _text_tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def _explicit_entity_match_boost(
    label: str,
    node_id: Any,
    node_data: dict[str, Any],
    signals: list[str],
    query: str | None,
) -> float:
    """Boost exact entity-name requests before broader tag matches."""
    aliases = {
        label,
        _node_name(node_id),
        str(node_data.get("name") or ""),
    }
    signal_slugs = {"-".join(_text_tokens(signal)) for signal in signals}
    query_tokens = _text_tokens(query or " ".join(signals))
    query_raw = (query or "").lower()
    query_phrase = f" {' '.join(query_tokens)} " if query_tokens else ""

    boost = 0.0
    for alias in aliases:
        alias_tokens = _text_tokens(alias)
        if not alias_tokens:
            continue
        alias_slug = "-".join(alias_tokens)
        alias_phrase = " ".join(alias_tokens)
        is_single_token = len(alias_tokens) == 1
        exact_single_query = is_single_token and (
            signals == [alias_slug] or query_tokens == alias_tokens
        )
        if alias_slug in signal_slugs and (not is_single_token or exact_single_query):
            boost = max(boost, 2000.0 + (25.0 * len(alias_tokens)))
        if not is_single_token and alias_slug in query_raw:
            boost = max(boost, 1000.0 + (25.0 * len(alias_tokens)))
        if (
            not is_single_token
            and len(alias_tokens) <= 2
            and query_phrase
            and f" {alias_phrase} " in query_phrase
        ):
            boost = max(boost, 1000.0 + (25.0 * len(alias_tokens)))
    return boost


def recommend_by_tags_indexed(
    db_path: Path,
    tags: list[str],
    *,
    top_n: int = 10,
    query: str | None = None,
    entity_types: tuple[str, ...] | set[str] | None = None,
    min_normalized_score: float = 0.0,
    external_catalog_path: Path | None = None,
) -> tuple[list[dict[str, Any]], int] | None:
    """Rank from a validated graph-store snapshot without loading graph JSON.

    The store supplies a complete candidate set, exact query-token IDF values,
    and original graph degrees. The existing ``recommend_by_tags`` scorer then
    applies the public ranking contract. ``None`` means the index cannot be
    trusted and the caller must fall back to the source graph.
    """
    signals = _normalise_signals(tags)
    if top_n < 1 or not signals:
        return ([], 0)
    if not db_path.is_file():
        return None

    try:
        with closing(_open_recommendation_index(db_path)) as conn:
            metadata = _recommendation_index_metadata(conn)
            if metadata is None:
                return None
            total_nodes = int(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
            rows = _indexed_candidate_rows(conn, signals)
            candidates = [_indexed_node(row) for row in rows]
            non_external_nodes = total_nodes - _indexed_external_node_count(conn)
            idf = _indexed_signal_idf(candidates, signals, non_external_nodes)
            for node_id, node_data in candidates:
                node_data["_ctx_recommendation_degree"] = _indexed_node_degree(conn, node_id)
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error):
        return None

    import networkx as nx  # noqa: PLC0415

    graph = nx.Graph()
    graph.graph.update(_indexed_graph_metadata(metadata))
    graph.graph["_ctx_recommendation_graph_marker"] = _INDEXED_RECOMMENDATION_GRAPH_MARKER
    graph.graph["_ctx_recommendation_token_idf"] = idf
    graph.add_nodes_from(candidates)
    return (
        recommend_by_tags(
            graph,
            signals,
            top_n=top_n,
            query=query,
            entity_types=entity_types,
            min_normalized_score=min_normalized_score,
            use_semantic_query=False,
            external_catalog_path=external_catalog_path,
        ),
        total_nodes,
    )


def resolve_recommendation_aliases_indexed(
    db_path: Path,
    *,
    typed_ids: list[str],
    bare_labels: list[str],
    allowed_entity_types: tuple[str, ...],
) -> tuple[dict[str, str], int] | None:
    """Resolve typed IDs and unambiguous labels from a graph-store snapshot."""
    typed = {value.strip().lower() for value in typed_ids if value.strip()}
    labels = {value.strip().lower() for value in bare_labels if value.strip()}
    if not db_path.is_file():
        return None
    try:
        with closing(_open_recommendation_index(db_path)) as conn:
            if _recommendation_index_metadata(conn) is None:
                return None
            total_nodes = int(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
            if not typed and not labels:
                return {}, total_nodes
            clauses: list[str] = []
            params: list[str] = []
            if typed:
                placeholders = ",".join("?" for _ in typed)
                clauses.append(f"lower(id) IN ({placeholders})")
                params.extend(sorted(typed))
            if labels:
                placeholders = ",".join("?" for _ in labels)
                clauses.append(
                    "("
                    f"lower(label) IN ({placeholders}) OR "
                    f"lower(COALESCE(json_extract(attrs_json, '$.name'), '')) "
                    f"IN ({placeholders})"
                    ")"
                )
                ordered_labels = sorted(labels)
                params.extend(ordered_labels)
                params.extend(ordered_labels)
            rows = conn.execute(
                "SELECT id, label, attrs_json FROM nodes WHERE " + " OR ".join(clauses),
                params,
            ).fetchall()
    except (OSError, ValueError, json.JSONDecodeError, sqlite3.Error):
        return None

    allowed = set(allowed_entity_types)
    typed_matches: dict[str, str] = {}
    label_matches: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        node_id = str(row["id"])
        entity_type = node_id.split(":", 1)[0].lower()
        if entity_type not in allowed:
            continue
        attrs = json.loads(str(row["attrs_json"]))
        if not isinstance(attrs, dict):
            return None
        typed_key = node_id.lower()
        if typed_key in typed:
            typed_matches[typed_key] = node_id
        label = str(attrs.get("label") or attrs.get("name") or "").strip().lower()
        if label in labels:
            label_matches[label].append(node_id)
    resolved = dict(typed_matches)
    resolved.update(
        {label: matches[0] for label, matches in label_matches.items() if len(matches) == 1}
    )
    return resolved, total_nodes


def _open_recommendation_index(db_path: Path) -> sqlite3.Connection:
    from ctx.core.graph.graph_store import open_graph_store_readonly  # noqa: PLC0415

    return open_graph_store_readonly(db_path)


def _recommendation_index_metadata(conn: sqlite3.Connection) -> dict[str, str] | None:
    rows = conn.execute("SELECT key, value FROM metadata").fetchall()
    metadata = {str(row["key"]): str(row["value"]) for row in rows}
    if metadata.get("schema_version") != "1":
        return None
    return metadata


def _indexed_candidate_rows(
    conn: sqlite3.Connection,
    signals: list[str],
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[str] = []
    for signal in signals:
        clauses.append(
            "("
            "instr(search_text, ?) > 0 OR "
            "instr(lower(COALESCE(json_extract(attrs_json, '$.name'), '')), ?) > 0"
            ")"
        )
        params.extend((signal, signal))
    return conn.execute(
        """
        SELECT id, type, label, tags_json, attrs_json
        FROM nodes
        WHERE
        """
        + " OR ".join(clauses),
        params,
    ).fetchall()


def _indexed_node(row: sqlite3.Row) -> tuple[str, dict[str, Any]]:
    attrs = json.loads(str(row["attrs_json"]))
    tags = json.loads(str(row["tags_json"]))
    if not isinstance(attrs, dict) or not isinstance(tags, list):
        raise ValueError("invalid graph-store node payload")
    attrs.setdefault("type", row["type"] or "skill")
    attrs.setdefault("label", row["label"] or str(row["id"]).split(":", 1)[-1])
    attrs["tags"] = tags
    return str(row["id"]), attrs


def _indexed_external_node_count(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT type, attrs_json
        FROM nodes
        WHERE type = 'external-skill'
           OR json_type(attrs_json, '$.external') IS NOT NULL
        """
    ).fetchall()
    count = 0
    for row in rows:
        attrs = json.loads(str(row["attrs_json"]))
        if not isinstance(attrs, dict):
            raise ValueError("invalid graph-store node attributes")
        if row["type"] == "external-skill" or attrs.get("external"):
            count += 1
    return count


def _indexed_signal_idf(
    candidates: list[tuple[str, dict[str, Any]]],
    signals: list[str],
    total_nodes: int,
) -> dict[str, float]:
    if total_nodes <= 0:
        return {}
    signal_set = set(signals)
    document_frequency = dict.fromkeys(signals, 0)
    for node_id, data in candidates:
        if data.get("external") or data.get("type") == "external-skill":
            continue
        label = str(data.get("label") or _node_name(node_id))
        tokens = set(_slug_tokens(label))
        for tag in data.get("tags", []):
            tokens.update(_slug_tokens(str(tag)))
        for token in signal_set & tokens:
            if len(token) >= 3:
                document_frequency[token] += 1
    return {
        token: math.log(total_nodes / frequency)
        for token, frequency in document_frequency.items()
        if len(token) >= 3 and frequency > 0
    }


def _indexed_node_degree(conn: sqlite3.Connection, node_id: str) -> int:
    row = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM edges INDEXED BY idx_edges_source WHERE source = ?)
          +
          (SELECT COUNT(*) FROM edges INDEXED BY idx_edges_target WHERE target = ?)
        """,
        (node_id, node_id),
    ).fetchone()
    return int(row[0])


def _indexed_graph_metadata(metadata: dict[str, str]) -> dict[str, Any]:
    graph_metadata: dict[str, Any] = {}
    for key in ("ctx_graph_path", "external_catalog_nodes", "source_catalog_nodes"):
        value = metadata.get(key)
        if value is None:
            continue
        if key.endswith("_nodes"):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                continue
        graph_metadata[key] = value
    return graph_metadata


def recommend_by_tags(
    graph: Any,
    tags: list[str],
    *,
    top_n: int = 10,
    query: str | None = None,
    entity_types: tuple[str, ...] | set[str] | None = None,
    min_normalized_score: float = 0.0,
    semantic_cache_dir: Path | None = None,
    semantic_weight: float = 100.0,
    use_semantic_query: bool = False,
    external_catalog_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Rank graph entities by name match, tag overlap, and graph degree.

    Scoring (for a query signal ``s`` and a node label ``L``):

      - **Exact slug-token match** (``s`` ∈ slug-tokens of ``L``):
        ``50 × (1 + IDF(s))``. The IDF multiplier is the key fix for
        the "python beats fastapi" failure mode — common tokens score
        marginally over baseline, rare tokens score 5-7× higher.
      - **Substring match** (``s`` appears inside a slug-token but is
        not itself a token): ``20`` (no IDF). Catches partial matches
        without letting them dominate.
      - **Tag overlap**: ``+10 × (1 + IDF(tag))`` per matching tag.
      - **Graph centrality**: ``+log(1 + degree)`` to break ties in
        favour of well-connected entities.
      - **Explicit entity-name match**: a large boost when the query
        names an entity exactly, either as a slug or a natural-language
        phrase.

    The IDF multiplier on tag matches mirrors the slug-token pass —
    a rare tag like ``rust`` should weigh more than a common one
    like ``automation``.
    """
    signals = _normalise_signals(tags)
    has_semantic_query = bool(query and query.strip() and use_semantic_query)
    if top_n < 1 or (not signals and not has_semantic_query):
        return []
    entity_type_filter = {str(t) for t in entity_types} if entity_types else None
    min_score = max(0.0, min(1.0, float(min_normalized_score)))

    idf = _token_idf(graph)

    # Optional: precompute per-node semantic similarity to the query.
    # When ``query`` is provided AND the embedding cache is current,
    # we embed the query with the same model graphify used and score
    # each node by cosine. Falls through silently to tag+token-only
    # ranking when the cache is missing or the embedder isn't installed.
    sem_score: dict[str, float] = {}
    if query and use_semantic_query:
        sem_index = _load_semantic_index(graph, semantic_cache_dir)
        if sem_index is not None:
            mat, ordered_ids, model_id = sem_index
            qv = _embed_query(query, model_id)
            if qv is not None:
                try:
                    import numpy as np  # noqa: PLC0415

                    sims = mat @ qv  # (N,) cosines in [-1, 1]
                    # Floor at 0 — negative cosines just mean "not at all
                    # similar" and shouldn't pull score down.
                    sims = np.maximum(sims, 0.0)
                    sem_score = dict(zip(ordered_ids, sims.tolist()))
                except Exception:
                    sem_score = {}

    signal_set = set(signals)
    scored: list[tuple[str, float, str, dict[str, Any], set[str], set[str]]] = []
    for node_id, data in graph.nodes(data=True):
        node_data = dict(data)
        if _truthy_flag(node_data.get("never_load")):
            continue
        node_type = str(node_data.get("type", "skill"))
        if entity_type_filter is not None and node_type not in entity_type_filter:
            continue
        label = str(node_data.get("label") or _node_name(node_id))
        node_tags = {str(tag).lower() for tag in node_data.get("tags", [])}
        matching_tags = signal_set & node_tags

        score = 0.0
        label_lower = label.lower()
        slug_toks = _slug_tokens(label)
        for signal in signals:
            if signal in slug_toks:
                # Exact slug-token hit. Weight by IDF so rare tokens
                # ('fastapi') dominate over common ones ('python').
                score += 50.0 * (1.0 + idf.get(signal, 0.0))
            elif signal in label_lower:
                # Substring-only fallback. Lower weight, no IDF —
                # substring matches are inherently fuzzier.
                score += 20.0
        for tag in matching_tags:
            score += 10.0 * (1.0 + idf.get(tag, 0.0))

        score += _explicit_entity_match_boost(
            label,
            node_id,
            node_data,
            signals,
            query,
        )

        # Semantic boost: cosine ∈ [0, 1] × weight. With weight=100
        # and a strong cosine of 0.7, this contributes 70 — roughly
        # the same magnitude as a single high-IDF slug-token hit.
        # Tunable via ``semantic_weight``.
        if sem_score:
            score += sem_score.get(node_id, 0.0) * semantic_weight

        if score <= 0:
            continue

        score += math.log1p(_recommendation_degree(graph, node_id, node_data))
        scored.append((label, score, node_id, node_data, matching_tags, node_tags))

    ranked = sorted(
        scored,
        key=lambda item: (
            -item[1],
            str(item[3].get("type", "skill")),
            item[0].lower(),
            item[2],
        ),
    )
    top_score = ranked[0][1] if ranked else 0.0
    graph_results: list[dict[str, Any]] = []
    for label, score, _node_id, node_data, matching_tags, node_tags in ranked:
        normalized_score = round(score / top_score, 4) if top_score else 0.0
        if normalized_score < min_score:
            continue
        graph_results.append(
            {
                "name": _public_label(label),
                "type": node_data.get("type", "skill"),
                "score": round(score, 1),
                "normalized_score": normalized_score,
                "matching_tags": sorted(matching_tags),
                "tags": sorted(node_tags),
                "external": node_data.get("external", False),
                "external_catalog": node_data.get("external_catalog"),
                "source_catalog": _public_source_catalog(node_data.get("source_catalog")),
                "status": _public_status(node_data.get("status")),
                "never_load": _truthy_flag(node_data.get("never_load")),
                "source": node_data.get("source"),
                "skill_id": node_data.get("skill_id"),
                "installs": _safe_int(node_data.get("installs")),
                "detail_url": node_data.get("detail_url"),
                "install_command": node_data.get("install_command"),
                "category": node_data.get("category"),
                "invoke_command": node_data.get("invoke_command"),
                "security_review": node_data.get("security_review"),
            }
        )
        if len(graph_results) >= top_n:
            break
    external_results: list[dict[str, Any]] = []
    include_external_skills = entity_type_filter is None or "skill" in entity_type_filter
    if include_external_skills and not _graph_has_external_catalog_nodes(graph, "skills.sh"):
        external_results = _recommend_external_catalog(
            graph,
            signals,
            top_n=top_n,
            query=query,
            catalog_path=external_catalog_path,
        )
    if not external_results:
        return graph_results
    merged = sorted(
        [*graph_results, *external_results],
        key=lambda item: (
            -float(item.get("score", 0.0)),
            str(item.get("type", "skill")),
            str(item.get("name", "")).lower(),
        ),
    )
    merged_top = float(merged[0].get("score", 0.0)) if merged else 0.0
    filtered: list[dict[str, Any]] = []
    if merged_top > 0:
        for item in merged:
            item["normalized_score"] = round(float(item.get("score", 0.0)) / merged_top, 4)
            if float(item["normalized_score"]) >= min_score:
                filtered.append(item)
            if len(filtered) >= top_n:
                break
    return filtered


def _graph_has_external_catalog_nodes(graph: Any, catalog_name: str) -> bool:
    try:
        external_counts = graph.graph.get("external_catalog_nodes")
        source_counts = graph.graph.get("source_catalog_nodes")
    except AttributeError:
        return False
    if isinstance(external_counts, dict):
        if _safe_int(external_counts.get(catalog_name)) > 0:
            return True
    if isinstance(source_counts, dict):
        return _safe_int(source_counts.get(catalog_name)) > 0
    return False


@functools.lru_cache(maxsize=8)
def _load_external_catalog_cached(
    path_str: str, mtime_ns: int, size: int
) -> tuple[dict[str, Any], ...]:
    del mtime_ns, size  # cache-key salt; the values are not needed inside the loader.
    path = Path(path_str)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ()
    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, list):
        return ()
    return tuple(s for s in skills if isinstance(s, dict))


def _infer_external_catalog_path(graph: Any) -> Path | None:
    graph_path = None
    try:
        graph_path = graph.graph.get("ctx_graph_path")
    except AttributeError:
        graph_path = None
    if not graph_path:
        return None
    path = Path(str(graph_path))
    return path.parent.parent / "external-catalogs" / "skills-sh" / "catalog.json"


def _load_external_catalog(path: Path | None) -> tuple[dict[str, Any], ...]:
    if path is None or not path.is_file():
        return ()
    try:
        stat = path.stat()
    except OSError:
        return ()
    return _load_external_catalog_cached(str(path), stat.st_mtime_ns, stat.st_size)


def _recommend_external_catalog(
    graph: Any,
    signals: list[str],
    *,
    top_n: int,
    query: str | None,
    catalog_path: Path | None,
) -> list[dict[str, Any]]:
    """Rank shipped skill-index entries alongside graph entities.

    These entries carry install instructions and detail URLs,
    but they do not pretend to be local ``converted/<slug>/SKILL.md`` wiki
    bodies. This keeps the graph useful while making the shipped skill index
    available as a fallback recommendation source.
    """
    path = catalog_path or _infer_external_catalog_path(graph)
    skills = _load_external_catalog(path)
    if not skills:
        return []

    signal_set = set(signals)
    query_l = (query or " ".join(signals)).lower()
    scored: list[tuple[float, dict[str, Any], set[str], set[str]]] = []
    for skill in skills:
        name = str(skill.get("name") or skill.get("skill_id") or "")
        full_id = str(skill.get("id") or "")
        source = str(skill.get("source") or "")
        skill_id = str(skill.get("skill_id") or "")
        tags = {str(t).lower() for t in skill.get("tags", []) if t}
        haystack = " ".join([name, full_id, source, skill_id, " ".join(sorted(tags))]).lower()
        slug_toks = {tok for tok in re.findall(r"[a-z0-9]+", haystack) if tok}
        matching = signal_set & tags

        score = 0.0
        for signal in signals:
            if signal in slug_toks:
                score += 42.0
            elif signal in haystack:
                score += 14.0
        score += 16.0 * len(matching)
        if query_l and query_l in haystack:
            score += 35.0
        installs = _safe_int(skill.get("installs"))
        if installs > 0:
            score += min(math.log10(installs + 1) * 8.0, 48.0)
        if score <= 0:
            continue
        scored.append((score, skill, matching, tags))

    ranked = sorted(scored, key=lambda item: -item[0])[:top_n]
    return [
        {
            "name": str(skill.get("id") or skill.get("name") or ""),
            "type": "skill",
            "score": round(score, 1),
            "normalized_score": 0.0,
            "matching_tags": sorted(matching),
            "tags": sorted(tags),
            "external": False,
            "external_catalog": None,
            "source_catalog": "skill-index",
            "status": "available",
            "source": skill.get("source"),
            "skill_id": skill.get("skill_id"),
            "installs": _safe_int(skill.get("installs")),
            "detail_url": skill.get("detail_url"),
            "install_command": skill.get("install_command"),
            "category": skill.get("category"),
            "invoke_command": skill.get("invoke_command"),
            "security_review": skill.get("security_review"),
        }
        for score, skill, matching, tags in ranked
    ]


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _public_source_catalog(value: Any) -> Any:
    if str(value).strip().lower() == "skills.sh":
        return "skill-index"
    return value


def _public_label(value: Any) -> str:
    label = str(value)
    if label.lower().startswith("skills-sh-"):
        return label[len("skills-sh-") :]
    return label


def _public_status(value: Any) -> Any:
    if str(value).strip().lower() == "remote-cataloged":
        return "available"
    return value


def _normalise_signals(tags: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for tag in tags:
        signal = str(tag).strip().lower()
        if signal:
            seen.setdefault(signal, None)
            if len(seen) >= _MAX_RECOMMENDATION_SIGNALS:
                break
    return list(seen.keys())


def _node_name(node_id: Any) -> str:
    return str(node_id).split(":", 1)[-1]
