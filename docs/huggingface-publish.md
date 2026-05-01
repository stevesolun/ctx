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

Set the token in the process environment. Do not pass it on a command line
that will be saved in shell history.

```powershell
$env:HF_TOKEN = "<hugging-face-write-token>"
python -m pip install --upgrade huggingface_hub
git lfs install
git lfs pull --include="graph/wiki-graph.tar.gz"
@'
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from huggingface_hub import HfApi

root = Path.cwd()
token = os.environ["HF_TOKEN"]
api = HfApi(token=token)
owner = api.whoami()["name"]
repo_id = f"{owner}/ctx"
repo_type = "dataset"
sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
files = [
    Path(raw.decode("utf-8"))
    for raw in subprocess.check_output(["git", "ls-files", "-z"], cwd=root).split(b"\0")
    if raw
]
graph_tar = root / "graph" / "wiki-graph.tar.gz"
if not graph_tar.is_file() or graph_tar.stat().st_size < 100_000_000:
    raise SystemExit(
        "graph/wiki-graph.tar.gz is not hydrated; run git lfs pull before publishing"
    )

api.create_repo(repo_id=repo_id, repo_type=repo_type, private=False, exist_ok=True, token=token)
staging = Path(tempfile.mkdtemp(prefix="ctx-hf-upload-"))
try:
    for rel in files:
        src = root / rel
        if not src.is_file():
            continue
        dst = staging / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    api.upload_folder(
        repo_id=repo_id,
        repo_type=repo_type,
        folder_path=staging,
        commit_message=f"Publish ctx snapshot {sha}",
        token=token,
    )
finally:
    shutil.rmtree(staging, ignore_errors=True)
'@ | python -
```

Then upload the dataset-card metadata wrapper for `README.md`:

```powershell
$env:HF_TOKEN = "<hugging-face-write-token>"
@'
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from huggingface_hub import HfApi

token = os.environ["HF_TOKEN"]
api = HfApi(token=token)
repo_id = f"{api.whoami()['name']}/ctx"
frontmatter = """---
license: mit
tags:
- agents
- mcp
- skills
- knowledge-graph
- llm-wiki
- recommendation-system
- harness
- codex
- claude-code
pretty_name: ctx
---

"""
with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as fh:
    path = Path(fh.name)
    fh.write(frontmatter)
    fh.write(Path("README.md").read_text(encoding="utf-8"))
try:
    api.upload_file(
        repo_id=repo_id,
        repo_type="dataset",
        path_or_fileobj=path,
        path_in_repo="README.md",
        commit_message="Add Hugging Face dataset card metadata",
        token=token,
    )
finally:
    path.unlink(missing_ok=True)
'@ | python -
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
