from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
for path in (SRC, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import backup_config as bc  # noqa: E402
import pack_full_wiki_tar  # noqa: E402
import sync_huggingface  # noqa: E402
from ctx.core.wiki import wiki_queue_worker  # noqa: E402


class _FakeCommitInfo:
    commit_url = "https://huggingface.co/datasets/Stevesolun/ctx/commit/card"


class _FakeHfApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def upload_folder(self, **kwargs: Any) -> _FakeCommitInfo:
        self.calls.append(("upload_folder", kwargs))
        return _FakeCommitInfo()


def test_artifact_promotion_retry_allows_completed_promotion_recovery(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    target = wiki / "graphify-out" / "graph.json"
    target.parent.mkdir(parents=True)
    payload = b'{"nodes":[]}\n'
    target.write_bytes(payload)
    staged = target.with_name(f"{target.name}.staged")
    metadata = target.with_name(f"{target.name}.promotion.json")
    metadata.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "staged",
                "target": str(target),
                "previous": {},
                "candidate": {"sha256": sha256(payload).hexdigest()},
            }
        ),
        encoding="utf-8",
    )

    message = wiki_queue_worker._handle_artifact_promotion(
        wiki,
        {
            "staged_path": staged.name,
            "target_path": "graphify-out/graph.json",
        },
    )

    assert message == f"promoted artifact to {target}"
    assert json.loads(metadata.read_text(encoding="utf-8"))["status"] == "promoted"


def test_from_ctx_config_ignores_malformed_user_top_files(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "backup-config.json").write_text(
        json.dumps({"top_files": ["safe.json", "../secret.json", "nested/file.json"]}),
        encoding="utf-8",
    )

    cfg = bc.from_ctx_config()

    assert cfg.top_files == ("safe.json",)
    assert "ignoring top_files entry" in capsys.readouterr().err


def test_host_user_path_redaction_covers_paths_with_spaces() -> None:
    text = (
        "POSIX `/Users/steves/My Project/wiki page.md`\n"
        "Windows C:\\Users\\steves\\My Project\\wiki page.md\n"
    )

    redacted = pack_full_wiki_tar._redact_host_user_paths(text)

    assert "/Users/" not in redacted
    assert "C:\\Users" not in redacted
    assert "My Project" not in redacted
    assert redacted.count("<host-user-path>") == 2


def test_hf_card_export_uploads_docs_and_changelog(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    export_dir = tmp_path / "export"
    (repo / "docs").mkdir(parents=True)
    export_dir.mkdir()
    (repo / "README.md").write_text("# ctx\n", encoding="utf-8")
    (repo / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    (repo / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    monkeypatch.setattr(
        sync_huggingface,
        "_iter_tracked_files",
        lambda _repo: [
            Path("README.md"),
            Path("CHANGELOG.md"),
            Path("docs/guide.md"),
            Path("src/not-card.py"),
        ],
    )

    sync_huggingface._export_card_inputs(repo, export_dir)

    assert (export_dir / "README.md").read_text(encoding="utf-8").startswith("---\nlicense: mit\n")
    assert (export_dir / "CHANGELOG.md").read_text(encoding="utf-8") == "# Changelog\n"
    assert (export_dir / "docs" / "guide.md").read_text(encoding="utf-8") == "# Guide\n"
    assert not (export_dir / "src").exists()

    api = _FakeHfApi()
    url = sync_huggingface._upload_card_inputs(
        api=api,
        export_dir=export_dir,
        repo_id="Stevesolun/ctx",
        repo_type="dataset",
        head="abcdef1234567890",
    )

    assert url == "https://huggingface.co/datasets/Stevesolun/ctx/commit/card"
    assert api.calls == [
        (
            "upload_folder",
            {
                "repo_id": "Stevesolun/ctx",
                "repo_type": "dataset",
                "folder_path": str(export_dir),
                "commit_message": "Sync ctx card abcdef1",
                "commit_description": "GitHub commit: abcdef1234567890",
                "delete_patterns": list(sync_huggingface.CARD_ONLY_DELETE_PATTERNS),
            },
        )
    ]
