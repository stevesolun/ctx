"""
test_behavior_miner.py -- Regression tests for behavior_miner.

Covers:
- Intent log parsing: malformed JSON lines are dropped silently.
- Skill manifest parsing: missing/corrupt files yield empty counters.
- Signal extraction: co-invocation pairs, file-types, skill cadence.
- Commit-type parsing: Conventional Commits + "other" catch-all.
- Suggestion logic: MIN_EVIDENCE gate, share threshold for commit types,
  skip of chore/other, stable sort order by evidence.
- build_profile on an empty environment returns a usable profile.
- save_profile / load_profile round-trip preserves all fields.
- format_digest covers empty, no-suggestions, and many-suggestions cases.
- CLI: profile + suggest emit JSON / digest and persist when asked.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest

import behavior_miner as bm


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Create a real git repo with a controlled commit history."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, env=env)
    # Disable the repo's own hooks so init-time hooks don't interfere.
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath",
                    "/dev/null"], check=True, env=env)

    messages = [
        "feat: add login",
        "fix: typo in login",
        "feat(api): user endpoint",
        "docs: update README",
        "chore: bump deps",
        "refactor: rename foo",
        "garbage message without prefix",
        "feat: another feature",
    ]
    for i, msg in enumerate(messages):
        f = repo / f"f{i}.txt"
        f.write_text(str(i), encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", f.name],
                       check=True, env=env)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q",
             "--no-verify", "--allow-empty", "-m", msg],
            check=True, env=env,
        )
    return repo


@pytest.fixture()
def intent_log(tmp_path: Path) -> Path:
    """Build a small intent-log.jsonl with controlled signals."""
    p = tmp_path / "intent-log.jsonl"
    rows = [
        {"tool": "Bash", "signals": ["python", "docker"]},
        {"tool": "Bash", "signals": ["python", "docker"]},
        {"tool": "Bash", "signals": ["python", "docker", "fastapi"]},
        {"tool": "Bash", "signals": ["python"]},
        {"tool": "Bash", "signals": []},
    ]
    body = "\n".join(json.dumps(r) for r in rows)
    # Inject a malformed line mid-file to confirm silent skip
    body += "\nnot json at all\n"
    body += json.dumps({"tool": "Bash", "signals": ["python"]}) + "\n"
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture()
def skill_manifest(tmp_path: Path) -> Path:
    p = tmp_path / "skill-manifest.json"
    p.write_text(json.dumps({
        "load": [
            {"skill": "python-patterns", "source": "user"},
            {"skill": "python-patterns", "source": "user"},
            {"skill": "python-patterns", "source": "user"},
        ],
        "unload": [
            {"skill": "fastapi-pro", "source": "user"},
        ],
    }), encoding="utf-8")
    return p


# ── Source reader tests ─────────────────────────────────────────────────────


def test_iter_intent_events_drops_malformed(intent_log: Path):
    events = list(bm._iter_intent_events(intent_log))
    # 5 dict rows + 1 trailing valid row; the "not json" line is dropped.
    assert len(events) == 6
    assert all("signals" in e for e in events)


def test_iter_intent_events_missing_file(tmp_path: Path):
    assert list(bm._iter_intent_events(tmp_path / "nope.jsonl")) == []


def test_read_skill_manifest_missing(tmp_path: Path):
    assert bm._read_skill_manifest(tmp_path / "nope.json") == {}


def test_read_skill_manifest_corrupt(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    assert bm._read_skill_manifest(p) == {}


def test_git_commit_types_not_a_repo(tmp_path: Path):
    # Empty dir with no .git => empty list, not a crash
    assert bm._git_commit_types(tmp_path) == []


def test_git_commit_types_classifies(git_repo: Path):
    types = bm._git_commit_types(git_repo)
    c = Counter(types)
    # 3 feat, 1 fix, 1 docs, 1 chore, 1 refactor, 1 other
    assert c["feat"] == 3
    assert c["fix"] == 1
    assert c["docs"] == 1
    assert c["chore"] == 1
    assert c["refactor"] == 1
    assert c["other"] == 1


# ── Signal extraction tests ─────────────────────────────────────────────────


def test_extract_co_invocation_pairs(intent_log: Path):
    events = list(bm._iter_intent_events(intent_log))
    pairs = bm._extract_co_invocation(events)
    assert pairs[("docker", "python")] == 3  # 3 events had both
    assert pairs[("docker", "fastapi")] == 1
    assert pairs[("fastapi", "python")] == 1


def test_extract_co_invocation_ignores_single_signal_events():
    events = [{"signals": ["python"]}, {"signals": []}]
    assert bm._extract_co_invocation(events) == Counter()


def test_extract_file_types_flattens(intent_log: Path):
    events = list(bm._iter_intent_events(intent_log))
    c = bm._extract_file_types(events)
    assert c["python"] == 5
    assert c["docker"] == 3
    assert c["fastapi"] == 1


def test_extract_skill_cadence_counts_both_sides(skill_manifest: Path):
    manifest = bm._read_skill_manifest(skill_manifest)
    c = bm._extract_skill_cadence(manifest)
    assert c["python-patterns"] == 3
    assert c["fastapi-pro"] == 1


def test_extract_skill_cadence_tolerates_bad_entries():
    manifest = {
        "load": ["not-a-dict", {"skill": "ok"}, {"nope": "x"}],
        "unload": None,
    }
    c = bm._extract_skill_cadence(manifest)
    assert c["ok"] == 1
    assert "not-a-dict" not in c


# ── Suggestion tests ────────────────────────────────────────────────────────


def test_suggest_from_co_invocation_respects_min_evidence():
    # Only one pair clears MIN_EVIDENCE=3
    c = Counter({("a", "b"): 3, ("c", "d"): 2})
    suggestions = bm._suggest_from_co_invocation(c)
    assert len(suggestions) == 1
    assert suggestions[0].kind == "co-invocation"
    assert suggestions[0].proposed["name"] == "a-b-bundle"
    assert suggestions[0].evidence == 3


def test_suggest_from_commit_types_skips_chore_and_other():
    c = Counter({"feat": 10, "other": 20, "chore": 5})
    suggestions = bm._suggest_from_commit_types(c)
    kinds = [s.proposed["name"] for s in suggestions]
    assert "feat-review" in kinds
    assert "chore-review" not in kinds
    assert "other-review" not in kinds


def test_suggest_from_commit_types_respects_share_threshold():
    # feat=3 of 100 = 3%, under 15% threshold
    c = Counter({"feat": 3, "other": 97})
    assert bm._suggest_from_commit_types(c) == []


def test_suggest_from_skill_cadence_filters_low_evidence():
    c = Counter({"python-patterns": 5, "rare-skill": 1})
    suggestions = bm._suggest_from_skill_cadence(c)
    names = [s.proposed["name"] for s in suggestions]
    assert "python-patterns-default" in names
    assert "rare-skill-default" not in names


def test_suggest_from_file_types_filters_low_evidence():
    c = Counter({"python": 10, "rare": 1})
    suggestions = bm._suggest_from_file_types(c)
    names = [s.proposed["name"] for s in suggestions]
    assert "python-default" in names
    assert "rare-default" not in names


# ── build_profile ───────────────────────────────────────────────────────────


def test_build_profile_empty_environment(tmp_path: Path):
    profile = bm.build_profile(
        repo_root=tmp_path,
        intent_log=tmp_path / "missing.jsonl",
        skill_manifest=tmp_path / "missing.json",
        now=1000.0,
    )
    assert profile.total_intent_events == 0
    assert profile.total_commits == 0
    assert profile.suggestions == ()
    assert profile.generated_at == 1000.0


def test_build_profile_full(intent_log, skill_manifest, git_repo):
    profile = bm.build_profile(
        repo_root=git_repo,
        intent_log=intent_log,
        skill_manifest=skill_manifest,
        now=42.0,
    )
    assert profile.total_intent_events == 6
    assert profile.total_commits == 8  # 8 git commits
    # At least one suggestion should come out of the controlled data
    assert len(profile.suggestions) >= 1
    # Suggestions are sorted by evidence descending
    evs = [s.evidence for s in profile.suggestions]
    assert evs == sorted(evs, reverse=True)


# ── Persistence ─────────────────────────────────────────────────────────────


def test_save_and_load_profile_roundtrip(tmp_path: Path,
                                         intent_log, skill_manifest, git_repo):
    profile = bm.build_profile(
        repo_root=git_repo,
        intent_log=intent_log,
        skill_manifest=skill_manifest,
        now=42.0,
    )
    target = tmp_path / "profile.json"
    bm.save_profile(profile, target)
    loaded = bm.load_profile(target)
    assert loaded is not None
    assert loaded.total_intent_events == profile.total_intent_events
    assert loaded.total_commits == profile.total_commits
    assert loaded.generated_at == profile.generated_at
    assert len(loaded.suggestions) == len(profile.suggestions)
    # Sample one suggestion's structure
    if profile.suggestions:
        assert loaded.suggestions[0].kind == profile.suggestions[0].kind
        assert loaded.suggestions[0].evidence == profile.suggestions[0].evidence


def test_load_profile_missing_returns_none(tmp_path: Path):
    assert bm.load_profile(tmp_path / "nope.json") is None


def test_load_profile_corrupt_returns_none(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    assert bm.load_profile(p) is None


def test_save_profile_is_atomic_on_crash(tmp_path: Path, monkeypatch):
    profile = bm.build_profile(
        repo_root=tmp_path,
        intent_log=tmp_path / "missing.jsonl",
        skill_manifest=tmp_path / "missing.json",
        now=1.0,
    )
    target = tmp_path / "profile.json"
    # Write a known-good baseline
    bm.save_profile(profile, target)
    original = target.read_bytes()

    def boom(*a, **kw):
        raise OSError("simulated")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        bm.save_profile(profile, target)
    assert target.read_bytes() == original


# ── Digest rendering ────────────────────────────────────────────────────────


def test_format_digest_empty_environment():
    profile = bm.BehaviorProfile(
        total_intent_events=0, total_commits=0,
        co_invocation_pairs=(), skill_cadence=(),
        file_types=(), commit_types=(), suggestions=(),
        generated_at=0,
    )
    out = bm.format_digest(profile)
    assert "no behaviour yet" in out


def test_format_digest_no_new_suggestions():
    profile = bm.BehaviorProfile(
        total_intent_events=100, total_commits=10,
        co_invocation_pairs=(), skill_cadence=(),
        file_types=(), commit_types=(), suggestions=(),
        generated_at=0,
    )
    out = bm.format_digest(profile)
    assert "no new suggestions" in out


def test_format_digest_caps_limit():
    suggestions = tuple(
        bm.Suggestion(kind="file-type", rationale=f"r{i}",
                      evidence=10 - i, proposed={"name": f"t{i}"})
        for i in range(7)
    )
    profile = bm.BehaviorProfile(
        total_intent_events=100, total_commits=0,
        co_invocation_pairs=(), skill_cadence=(),
        file_types=(), commit_types=(),
        suggestions=suggestions, generated_at=0,
    )
    out = bm.format_digest(profile, limit=3)
    assert out.count("  - ") == 3
    assert "and 4 more" in out


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_profile_prints_json(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(bm, "INTENT_LOG", tmp_path / "missing.jsonl")
    monkeypatch.setattr(bm, "SKILL_MANIFEST", tmp_path / "missing.json")
    rc = bm.main(["profile", "--repo", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "suggestions" in payload
    assert payload["total_intent_events"] == 0


def test_cli_suggest_prints_digest(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(bm, "INTENT_LOG", tmp_path / "missing.jsonl")
    monkeypatch.setattr(bm, "SKILL_MANIFEST", tmp_path / "missing.json")
    rc = bm.main(["suggest", "--repo", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[toolbox]" in out


def test_cli_profile_save_persists(tmp_path: Path, monkeypatch, capsys):
    target = tmp_path / "profile.json"
    monkeypatch.setattr(bm, "INTENT_LOG", tmp_path / "missing.jsonl")
    monkeypatch.setattr(bm, "SKILL_MANIFEST", tmp_path / "missing.json")
    monkeypatch.setattr(bm, "USER_PROFILE", target)
    rc = bm.main(["profile", "--repo", str(tmp_path), "--save"])
    assert rc == 0
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert "suggestions" in payload
