# Delivery launcher repair — 2026-09-30

Canonical record: SEC-002, `AUDIT-20260930-CODEX-BUNDLE-DISCOVERY`.

The complete delivery run `01M3RV98HW4HQDSM04HA6NRBJG`, submitted at
`54dfe28ad1eef56a017ff7ea6985f1b7875bc52c`, completed intent and rebase,
then failed review before starting an agent: the wrapper exited 127 because
the current ChatGPT app stores Codex in a nested `CodexCLI.app` bundle.
No pipeline fixes, push, or PR occurred. The failed run is retained; earlier
green product gates do not prove this launcher path or the complete pipeline.

The coordinator reproduced the failure with:

```sh
env -i HOME=/Users/steves PATH=/usr/bin:/bin scripts/no_mistakes_codex_env.sh --version
```

The minimal repair extends the existing default candidates for system/user
Codex and ChatGPT bundles with their nested executable paths, retaining each
legacy fallback. Strict explicit-override validation, self-recursion rejection,
explicit-empty app-list opt-out, and PATH fallback are unchanged. CONTRIBUTING
now correctly says both non-empty executable/resource overrides are validated.

Evidence on the frozen repair:

- Writer captured a failing nested-path regression before the fix; the legacy
  case already passed. Both cases then passed.
- Coordinator replay: all 17 wrapper tests passed; `bash -n` and diff check
  passed; the exact stripped-environment command above returned exit 0 and
  `codex-cli 0.159.0`, with no model call.
- Independent reviewer accepted with no P0-P3 findings, independently replayed
  nine boundary cases and the real stripped-environment version command, and
  checked the unchanged precedence and validation semantics.
- The new parametrized test checks the default-list contract and isolated
  candidate execution. The real version-only probe, not that fixture alone,
  proves discovery through the installed app's default path.

Frozen SHA-256 values:

| File | SHA-256 |
| --- | --- |
| `scripts/no_mistakes_codex_env.sh` | `8d4110faab3cc12d9b9b243853163e7b7179f64b96c570e14947908b3d0b7e8d` |
| `src/tests/test_no_mistakes_env.py` | `9c9718eff7fc7f0096e606db07fa1820623a5cefb73ba9ae4e724b3409a767de` |
| `CONTRIBUTING.md` | `36a100c8addb6bffdf60e3a6e8e6fd84006741e98a6602bb731807734e7ed93b` |

SEC-002 remains **Needs Validation** until the actual complete delivery
sequence and CI pass. Neither focused tests nor this review close that story.
No global configuration, model invocation, new dependency, release, or remote
Support submission was needed for the repair.
