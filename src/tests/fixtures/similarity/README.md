# Similarity fixtures

Hand-written test corpus for the intake gate's similarity precision/recall harness
(`test_similarity_precision_recall.py`). Each JSONL line is one pair.

## Schema

```json
{
  "id": "nd-01",
  "label": "near_duplicate | distinct | adversarial",
  "a": {"name": "...", "description": "...", "body": "## Section\n..."},
  "b": {"name": "...", "description": "...", "body": "## Section\n..."},
  "note": "why this pair belongs to its label"
}
```

The test assembles full markdown via a shared helper so fixture files stay
focused on semantic content, not frontmatter boilerplate.

## Labels

- **near_duplicate** (30 pairs): same skill rewritten — paraphrased
  description, reordered sections, synonyms. Gate MUST flag
  `DUPLICATE` or `NEAR_DUPLICATE`.
- **distinct** (30 pairs): orthogonal domains. Gate MUST NOT flag.
- **adversarial** (10 pairs): same domain, genuinely different purpose
  (e.g. `python-pytest` vs `python-unittest`). Gate MUST NOT flag —
  these are the precision traps.

## Success criteria

Precision and recall both ≥ 0.90 across all three label sets. See test
docstring for the exact formulas.

## Editing

Pairs are independent — add, remove, or revise without side effects. Keep
bodies short (roughly 200-500 chars) so the suite runs in seconds but long
enough that MiniLM has enough signal to embed meaningfully.
