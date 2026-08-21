# Hugging Face Publish

ctx publishes the GitHub repository as the public Hugging Face dataset repo
[`Stevesolun/ctx`](https://huggingface.co/datasets/Stevesolun/ctx). The
dataset repo is a clean `git ls-files` snapshot plus the two exact archives
declared by `graph/release-artifacts.json`, not local review reports or ignored
caches.

## What gets uploaded

- Tracked source, docs, tests, and packaging files.
- `graph/wiki-graph.tar.gz`.
- `graph/wiki-graph-runtime.tar.gz`.
- The compressed skill index under `graph/`.
- Tracked graph visualizations under `graph/`.

Ignored local reports, review notes, raw ingest caches, coverage files,
`site/`, and `.pytest_cache/` are not uploaded because they are not tracked
by git.

## Automatic publish

Every push to `main` runs `.github/workflows/huggingface-sync.yml`. The job
checks out source, strictly validates `graph/release-artifacts.json`, and
hydrates only its two large archives from the named GitHub release. It verifies
all five graph assets by exact size and SHA-256, with no Git LFS fallback. It
then installs the sync dependencies and calls
`scripts/sync_huggingface.py`. It publishes only when the repository secret
`HF_TOKEN` is configured. On the canonical
`stevesolun/ctx` repository, a missing token is a hard failure so main cannot
silently drift from the dataset repo. On forks, a missing token still exits
successfully with a notice because forks are not trusted publishing sources.

When only repo-card inputs changed (`README.md`, `CHANGELOG.md`, or files under
`docs/`), the workflow uses card-only upload mode. Source, test, workflow, graph,
or packaging changes always run the full dataset sync so tracked files cannot
drift behind GitHub.

The sync script is still the contract: it exports the tracked git snapshot,
adds the two manifest-declared archives, adds Hugging Face repo-card metadata,
validates README/docs stats, and refuses missing, stale, or corrupt artifacts.

## Manual publish

Use the repository sync script. It exports tracked files plus the validated
local graph artifacts, adds the Hugging Face repo-card frontmatter to the
uploaded `README.md`, and refuses to publish if the manifest or any full wiki,
runtime wiki, or compressed skill-index byte identity is missing or mismatched.

Full sync uploads the exported tree with `delete_patterns="*"`, so files removed
from the current git snapshot are removed remotely in the same commit.
Card-only sync exports only `README.md`, `CHANGELOG.md`, and tracked `docs/**`
inputs, then replaces just those remote paths.

Do not paste the token into a command line. Prompt for it, set it only for the
current process, and clear it after the upload.

```bash
python -m pip install --upgrade huggingface_hub

read -rsp "HF write token: " HF_TOKEN
printf '\n'
export HF_TOKEN
trap 'unset HF_TOKEN' EXIT
python scripts/sync_huggingface.py --repo . --repo-id Stevesolun/ctx --repo-type dataset
unset HF_TOKEN
trap - EXIT
```

For a README/changelog/docs-only refresh:

```bash
python scripts/sync_huggingface.py --repo . --repo-id Stevesolun/ctx --repo-type dataset --card-only
```

## Verify

```bash
python - <<'PY'
from huggingface_hub import HfApi

api = HfApi()
info = api.repo_info(repo_id="Stevesolun/ctx", repo_type="dataset")
print(info.id, info.sha)
PY
```

The dataset page should show the MIT license and the tags from the metadata
wrapper.
