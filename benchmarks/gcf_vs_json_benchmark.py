#!/usr/bin/env python3
"""Benchmark: GCF (Graph Compact Format) vs JSON token cost for ctx MCP tool responses.

Generates realistic ctx recommendation, graph query, and wiki search payloads
at varying result counts and compares JSON vs GCF encoding sizes using a
len/4 token heuristic.
"""

from __future__ import annotations

import json
import random
import string
import sys
from pathlib import Path

try:
    from gcf import encode_generic
except ImportError:
    sys.exit("gcf-python is not installed. Run: pip install gcf-python")


# ── Realistic data generators ────────────────────────────────────────────────

_TAGS = [
    "python", "typescript", "rust", "go", "docker", "kubernetes", "aws",
    "gcp", "azure", "ci-cd", "testing", "linting", "security", "database",
    "api", "graphql", "rest", "grpc", "monitoring", "logging", "caching",
    "auth", "oauth", "jwt", "websocket", "streaming", "ml", "nlp", "llm",
    "embeddings", "vector-db", "postgres", "redis", "elasticsearch",
]

_ENTITY_TYPES = ["skill", "agent", "mcp-server"]

_STATUSES = ["stable", "beta", "experimental", "deprecated"]

_CATEGORIES = [
    "code-quality", "deployment", "infrastructure", "data-pipeline",
    "developer-tools", "testing", "security", "documentation",
]


def _random_slug(prefix: str = "") -> str:
    base = prefix or random.choice(["auto", "smart", "fast", "deep", "multi"])
    suffix = "".join(random.choices(string.ascii_lowercase, k=random.randint(4, 8)))
    return f"{base}-{suffix}"


def _random_url() -> str:
    return f"https://github.com/example/{_random_slug()}"


def _generate_recommend_result() -> dict:
    name = _random_slug()
    return {
        "name": name,
        "type": random.choice(_ENTITY_TYPES),
        "score": round(random.uniform(0.3, 1.0), 4),
        "normalized_score": round(random.uniform(0.5, 1.0), 4),
        "matching_tags": random.sample(_TAGS, k=random.randint(2, 6)),
        "external": random.choice([True, False]),
        "source_catalog": random.choice(["community", "official", "curated", None]),
        "status": random.choice(_STATUSES),
        "source": random.choice(["wiki", "registry", "marketplace"]),
        "skill_id": f"sk-{name}-{''.join(random.choices(string.hexdigits[:16], k=8))}",
        "installs": random.randint(10, 50000),
        "detail_url": _random_url(),
        "install_command": f"ctx-skill-install {name}",
        "category": random.choice(_CATEGORIES),
        "invoke_command": f"ctx run --skill {name}",
        "security_review": random.choice(["passed", "pending", "not_reviewed", None]),
    }


def _generate_recommend_bundle(n: int) -> dict:
    return {
        "query": "kubernetes deployment automation with security scanning",
        "tags": random.sample(_TAGS, k=min(5, len(_TAGS))),
        "results": [_generate_recommend_result() for _ in range(n)],
        "companion_harnesses": [],
    }


def _generate_graph_result() -> dict:
    return {
        "name": _random_slug(),
        "type": random.choice(_ENTITY_TYPES),
        "score": round(random.uniform(0.1, 1.0), 4),
        "normalized_score": round(random.uniform(0.3, 1.0), 4),
        "shared_tags": random.sample(_TAGS, k=random.randint(1, 4)),
        "via": [_random_slug() for _ in range(random.randint(0, 3))],
    }


def _generate_graph_query(n: int) -> dict:
    return {
        "seeds": [_random_slug() for _ in range(random.randint(1, 3))],
        "results": [_generate_graph_result() for _ in range(n)],
    }


def _generate_wiki_hit() -> dict:
    name = _random_slug()
    return {
        "slug": name,
        "title": name.replace("-", " ").title(),
        "entity_type": random.choice(_ENTITY_TYPES),
        "wikilink": f"[[{random.choice(_ENTITY_TYPES)}:{name}]]",
        "description": f"A {random.choice(_CATEGORIES)} tool for {random.choice(_TAGS)} workflows.",
        "excerpt": (
            "Automates common tasks in the development pipeline "
            "with built-in support for multiple providers and "
            "extensible plugin architecture."
        ),
        "tags": random.sample(_TAGS, k=random.randint(3, 7)),
        "status": random.choice(_STATUSES),
        "score": round(random.uniform(0.2, 1.0), 4),
    }


def _generate_wiki_search(n: int) -> dict:
    return {
        "query": "security scanning and code analysis tools",
        "results": [_generate_wiki_hit() for _ in range(n)],
    }


# ── Benchmark runner ─────────────────────────────────────────────────────────

def _token_estimate(text: str) -> int:
    """Approximate token count using len/4 heuristic."""
    return len(text) // 4


def run_benchmark() -> str:
    random.seed(42)  # reproducible results

    sizes = [5, 10, 15, 25]
    generators = {
        "recommend_bundle": _generate_recommend_bundle,
        "graph_query": _generate_graph_query,
        "wiki_search": _generate_wiki_search,
    }

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("GCF vs JSON Token Benchmark for ctx MCP Tool Responses")
    lines.append("=" * 72)
    lines.append("")

    total_json_tokens = 0
    total_gcf_tokens = 0

    for gen_name, gen_fn in generators.items():
        lines.append(f"--- {gen_name} ---")
        lines.append(
            f"{'Results':>8}  {'JSON tokens':>12}  {'GCF tokens':>11}  "
            f"{'Savings':>8}  {'Reduction':>10}"
        )
        for size in sizes:
            data = gen_fn(size)
            json_str = json.dumps(data, indent=2)
            gcf_str = encode_generic(data)

            json_tok = _token_estimate(json_str)
            gcf_tok = _token_estimate(gcf_str)
            saved = json_tok - gcf_tok
            pct = (saved / json_tok * 100) if json_tok > 0 else 0.0

            total_json_tokens += json_tok
            total_gcf_tokens += gcf_tok

            lines.append(
                f"{size:>8}  {json_tok:>12,}  {gcf_tok:>11,}  "
                f"{saved:>8,}  {pct:>9.1f}%"
            )
        lines.append("")

    total_saved = total_json_tokens - total_gcf_tokens
    total_pct = (total_saved / total_json_tokens * 100) if total_json_tokens > 0 else 0.0

    lines.append("=" * 72)
    lines.append("TOTALS (all payload types, all sizes)")
    lines.append(f"  JSON tokens:  {total_json_tokens:>10,}")
    lines.append(f"  GCF tokens:   {total_gcf_tokens:>10,}")
    lines.append(f"  Saved:        {total_saved:>10,}  ({total_pct:.1f}% reduction)")
    lines.append("=" * 72)
    lines.append("")
    lines.append("Token estimates use len/4 heuristic.")
    lines.append("GCF = Graph Compact Format (gcf-python >= 2.1.0)")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    output = run_benchmark()
    print(output)

    results_path = Path(__file__).parent / "results-2026-06-17.txt"
    results_path.write_text(output, encoding="utf-8")
    print(f"Results written to {results_path}")
