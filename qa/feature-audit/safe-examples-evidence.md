# Safe graph and telemetry example evidence

Tested commit: `2f7a6a23d7d6f80acc54c7c09543048367da5bc9`

Run from the repository root with the repository's development environment:

```bash
.venv/bin/python qa/feature-audit/safe_examples.py
```

The reproducer creates an isolated temporary home and deletes it at exit. It
does not contact the network, load or download a model, call a provider, start
a service, contact a telemetry collector, or write to the user's real home.

## Result

```json
{
  "attach_edges": 2,
  "compacted_graph_nodes": 3,
  "compacted_wiki_pages": 3,
  "external_calls": 0,
  "graphify": "Graph: 2 nodes, 1 edges",
  "index_nodes": 2,
  "telemetry_exported": 2,
  "telemetry_preview_attempted": 2,
  "telemetry_replayed": 2
}
```

## DOC-NAV003: graph build, index, attach, and compaction

The script executes the real command bodies with temporary paths:

```text
python -m ctx.core.wiki.wiki_graphify
  --wiki-dir <temporary-wiki>
  --incremental --graph-only --semantic-vector-index off

python -m ctx.core.graph.incremental_attach validate-indexes
  --index-dir <temporary-index> --json

python -m ctx.core.graph.incremental_attach attach
  --index-dir <temporary-index>
  --overlay <temporary-overlay>
  --node-id skill:python-helper --type skill --label python-helper
  --tag python --tag review --text-file <temporary-markdown>
  --vector-json [1.0,0.0] --model-id <generated-base-model-id>
  --min-final-weight 0.0 --pack-root <temporary-graph-packs>
  --base-export-id <generated-export-id>
  --parent-export-id <generated-export-id>
  --config-hash <generated-config-hash> --json

python -m ctx.core.wiki.pack_compaction compact
  --wiki-path <temporary-wiki>
  --base-export-id safe-example-compact-v1
  --staging-dir <temporary-stage> --json

python -m ctx.core.wiki.pack_compaction validate
  --staged-graph-packs-dir <temporary-stage>/graph-packs
  --staged-wiki-packs-dir <temporary-stage>/wiki-packs
  --require-compaction-manifest --json
```

Assertions prove that graphify writes the expected two nodes and one local
tag/token edge; the persisted NumPy-flat index validates with two nodes; attach
selects the two expected neighbors and writes a fixture overlay pack; and the
staged compacted base contains three graph nodes and three wiki pages with no
missing or orphan pages. Compaction is staged only and is not promoted.

The optional semantic embedding backend is deliberately omitted. The fixture
sets semantic graph weight to zero and uses fixed precomputed vectors through
the production vector-index API and the CLI's advanced `--vector-json` seam.
This proves persistence, validation, attach, overlay packing, and compaction;
it does not claim that an optional embedding model was exercised.

## DOC-NAV007: local telemetry preview and export

The script creates two events with the production `record_event` API, then
executes the real `ctx-telemetry-export` command body three times:

```text
ctx-telemetry-export --path <temporary-spool>
  --checkpoint <temporary-checkpoint>
  --sink local_jsonl --output <temporary-output> --dry-run --json

ctx-telemetry-export --path <temporary-spool>
  --checkpoint <temporary-checkpoint>
  --sink local_jsonl --output <temporary-incremental-output> --json

ctx-telemetry-export --path <temporary-spool>
  --checkpoint <temporary-checkpoint>
  --all --sink local_jsonl --output <temporary-replay-output> --json
```

The preview attempts two events and exports none. Incremental export writes
both events and advances its checkpoint. Full replay writes both events. The
reproducer asserts that both output files preserve exactly the well-formed
spool event IDs and that sensitive fixture query, token, and path values are
absent from the spool and both exports. Local JSONL intentionally retains its
local-only session identifier and should still be handled as sensitive.

Remote OTLP behavior remains outside this safe command example; its endpoint,
TLS, retry, redirect, and partial-success behavior is covered by isolated tests.
