# Hugging Face Publish

ctx publishes the GitHub repository as the public Hugging Face dataset
[`Stevesolun/ctx`](https://huggingface.co/datasets/Stevesolun/ctx). The
dataset is a clean `git ls-files` snapshot, including the shipped graph
tarball and catalog artifacts, not local review reports or ignored caches.

## What gets uploaded

- Tracked source, docs, tests, and packaging files.
- `graph/wiki-graph.tar.gz`.
- `graph/skills-sh-catalog.json.gz`.
- Tracked graph visualizations under `graph/`.

Ignored local reports, review notes, raw ingest caches, coverage files,
`site/`, and `.pytest_cache/` are not uploaded because they are not tracked
by git.

## Publish command

Use the repository sync script. It exports only tracked files, adds the
Hugging Face repo-card frontmatter to the uploaded `README.md`, and refuses to
publish if `graph/wiki-graph.tar.gz` or `graph/skills-sh-catalog.json.gz` is
missing, too small, or still a Git LFS pointer.

The script prefers Hugging Face's resumable large-folder uploader when the
remote already has no stale paths. If the remote contains files that are not in
the current git snapshot, the script falls back to a single clean replacement
commit so deleted local files cannot survive remotely.

Do not paste the token into a command line. Prompt for it, set it only for the
current process, and clear it after the upload.

```powershell
python -m pip install --upgrade huggingface_hub
git lfs install
git lfs pull --include="graph/wiki-graph.tar.gz,graph/skills-sh-catalog.json.gz"

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
