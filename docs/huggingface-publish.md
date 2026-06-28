# Hugging Face Publish

ctx publishes the GitHub repository as the public Hugging Face dataset repo
[`Stevesolun/ctx`](https://huggingface.co/datasets/Stevesolun/ctx). The
dataset repo is a clean `git ls-files` snapshot, including the shipped graph
tarball and catalog artifacts, not local review reports or ignored caches.

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
checks out source without spending Git LFS bandwidth, hydrates the required
graph artifacts from the latest GitHub release assets, installs the sync
dependencies, and calls `scripts/sync_huggingface.py`. It publishes only when
the repository secret `HF_TOKEN` is configured; otherwise it exits successfully
with a notice so public forks and dry repos do not fail.

The sync script is still the contract: it exports the tracked git snapshot,
adds Hugging Face repo-card metadata, validates README/docs stats, verifies the
graph artifacts are hydrated rather than LFS pointers, and refuses to publish
stale or corrupt artifacts.

## Manual publish

Use the repository sync script. It exports tracked files plus the validated
local graph artifacts, adds the Hugging Face repo-card frontmatter to the
uploaded `README.md`, and refuses to publish if the full wiki tarball, runtime
wiki tarball, or compressed skill index is missing, too small, or still a Git
LFS pointer.

The script prefers Hugging Face's resumable large-folder uploader when the
remote already has no stale paths. If the remote contains files that are not in
the current git snapshot, the script falls back to a single clean replacement
commit so deleted local files cannot survive remotely.

Do not paste the token into a command line. Prompt for it, set it only for the
current process, and clear it after the upload.

```powershell
python -m pip install --upgrade huggingface_hub

$secureToken = Read-Host "HF write token" -AsSecureString
$tokenPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
  $env:HF_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPtr)
  python scripts/sync_huggingface.py --repo . --repo-id Stevesolun/ctx --repo-type dataset
} finally {
  if ($tokenPtr -ne [IntPtr]::Zero) {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPtr)
  }
  Remove-Item Env:\HF_TOKEN -ErrorAction SilentlyContinue
}
```

## Verify

```powershell
@'
from huggingface_hub import HfApi

api = HfApi()
info = api.repo_info(repo_id="Stevesolun/ctx", repo_type="dataset")
print(info.id, info.sha)
'@ | python -
```

The dataset page should show the MIT license and the tags from the metadata
wrapper.
