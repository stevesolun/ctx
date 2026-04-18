#!/usr/bin/env python3
"""
intent_interview.py -- New-repo / existing-repo intent interview.

The "fresh-repo-init" toolbox exists so that an unfamiliar repo can bootstrap
its toolbox set without a human designing one from scratch. This module is
the brain behind that toolbox:

  1. Inspect repo state (git? commits? languages? existing toolbox config?).
  2. Build a structured question list from that state (plus any behaviour
     profile already on disk).
  3. Accept answers either interactively (stdin) or structurally (--accept
     key=value pairs, or --preset for a canned flow).
  4. Return an InterviewResult that a caller can apply to the global
     ToolboxSet via ``apply_result`` -- writes are explicit and atomic.

Design choices:
- *Three* answer paths per spec: interactive, structured (non-interactive),
  or skip entirely. No UI framework \u2014 plain stdin, plain argparse.
- The module produces an InterviewResult; it does not mutate global config
  unless ``apply_result`` is called explicitly. This keeps dry-runs safe.
- Suggestions come from the persisted BehaviorProfile (if any). The
  interviewer *surfaces* them but never auto-accepts \u2014 the user chooses.
- Starter templates are discovered via the existing toolbox.py loader so
  there is exactly one source of truth for what "fresh-repo-init" offers.

CLI:
  python intent_interview.py detect                   # print RepoState
  python intent_interview.py init                     # interactive flow
  python intent_interview.py init --non-interactive \\
      --starters ship-it,security-sweep \\
      --suggestions 1,3                               # pre-answered flow
  python intent_interview.py init --skip              # skip, write nothing
  python intent_interview.py init --preset blank      # auto blank-repo preset
  python intent_interview.py init --preset existing   # auto existing-repo preset
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

try:
    from behavior_miner import BehaviorProfile, build_profile, load_profile
    from toolbox_config import (
        Toolbox,
        ToolboxSet,
        global_config_path,
        load_global,
        merged,
        save_global,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).parent))
    from behavior_miner import (  # type: ignore[no-redef]
        BehaviorProfile, build_profile, load_profile,
    )
    from toolbox_config import (  # type: ignore[no-redef]
        Toolbox,
        ToolboxSet,
        global_config_path,
        load_global,
        merged,
        save_global,
    )


STARTER_NAMES = (
    "ship-it",
    "security-sweep",
    "refactor-safety",
    "docs-review",
    "fresh-repo-init",
)

# Map common file extensions onto our scope.signals vocabulary so a
# newly-cloned repo can be characterised without any prior intent-log.
_EXT_TO_SIGNAL: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "golang",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".sh": "bash",
    ".sql": "sql",
    ".tf": "terraform",
}

# If a marker file is present we can infer a richer signal than the
# extension alone gives us (e.g. pyproject.toml => python).
_MARKER_TO_SIGNAL: dict[str, str] = {
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "Pipfile": "python",
    "package.json": "javascript",
    "tsconfig.json": "typescript",
    "go.mod": "golang",
    "Cargo.toml": "rust",
    "pom.xml": "java",
    "build.gradle": "java",
    "build.gradle.kts": "kotlin",
    "Gemfile": "ruby",
    "composer.json": "php",
    "Dockerfile": "docker",
    "docker-compose.yml": "docker",
    "docker-compose.yaml": "docker",
    ".terraform": "terraform",
    "mkdocs.yml": "mkdocs",
}

MAX_FILES_FOR_LANG_SCAN = 200


# \u2500\u2500 Data model \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


@dataclass(frozen=True)
class RepoState:
    repo_root: str
    is_git_repo: bool
    commit_count: int
    tracked_file_count: int
    top_languages: tuple[tuple[str, int], ...]   # (signal, weight)
    has_toolbox_config: bool
    existing_active: tuple[str, ...]
    detected_markers: tuple[str, ...]

    @property
    def is_blank(self) -> bool:
        """
        A 'blank' repo = not a git repo at all, OR a git repo with zero
        commits, OR a git repo with commits but no discernible language
        signals and no existing toolbox config. Blank repos get the
        starter-picker flow; populated repos get the suggestion-first flow.
        """
        if not self.is_git_repo:
            return True
        if self.commit_count == 0:
            return True
        if not self.top_languages and not self.has_toolbox_config:
            return True
        return False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class InterviewQuestion:
    id: str
    prompt: str
    choices: tuple[tuple[str, str], ...]  # (value, label)
    multi: bool = False
    default: str | None = None


@dataclass(frozen=True)
class InterviewResult:
    activated: tuple[str, ...]
    accepted_suggestions: tuple[dict, ...]
    skipped: bool
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


# \u2500\u2500 State detection \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def _is_git_repo(repo_root: Path) -> bool:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "--is-inside-work-tree"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return out.strip() == "true"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _commit_count(repo_root: Path) -> int:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-list", "--count", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return int(out.strip() or 0)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return 0


def _tracked_files(repo_root: Path) -> list[str]:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_root), "ls-files"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _walk_files(repo_root: Path, limit: int) -> list[str]:
    """Fallback for non-git repos: walk top-level + one level down."""
    out: list[str] = []
    try:
        for entry in repo_root.iterdir():
            if entry.name.startswith((".git", ".venv", "__pycache__", "node_modules")):
                continue
            if entry.is_file():
                out.append(entry.name)
            elif entry.is_dir():
                try:
                    for sub in entry.iterdir():
                        if sub.is_file():
                            out.append(f"{entry.name}/{sub.name}")
                            if len(out) >= limit:
                                return out
                except OSError:
                    continue
            if len(out) >= limit:
                break
    except OSError:
        pass
    return out


def _score_languages(files: Iterable[str]) -> Counter:
    counter: Counter = Counter()
    for path in files:
        ext = Path(path).suffix.lower()
        sig = _EXT_TO_SIGNAL.get(ext)
        if sig:
            counter[sig] += 1
    return counter


def _detect_markers(repo_root: Path) -> tuple[list[str], set[str]]:
    found: list[str] = []
    signals: set[str] = set()
    for name, signal in _MARKER_TO_SIGNAL.items():
        if (repo_root / name).exists():
            found.append(name)
            signals.add(signal)
    return found, signals


def detect_state(repo_root: Path | None = None) -> RepoState:
    root = (repo_root or Path.cwd()).resolve()

    is_repo = _is_git_repo(root)
    commits = _commit_count(root) if is_repo else 0

    if is_repo:
        files = _tracked_files(root)[:MAX_FILES_FOR_LANG_SCAN]
    else:
        files = _walk_files(root, MAX_FILES_FOR_LANG_SCAN)

    lang_counter = _score_languages(files)
    markers, marker_signals = _detect_markers(root)
    for sig in marker_signals:
        # Markers weigh 5 each so they surface even when a repo has
        # few actual files (e.g. bootstrap-ready templates).
        lang_counter[sig] += 5

    top = tuple(lang_counter.most_common(5))

    tset = merged(repo_root=root)
    return RepoState(
        repo_root=str(root),
        is_git_repo=is_repo,
        commit_count=commits,
        tracked_file_count=len(files),
        top_languages=top,
        has_toolbox_config=bool(tset.toolboxes),
        existing_active=tset.active,
        detected_markers=tuple(markers),
    )


# \u2500\u2500 Question construction \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def _starter_choices(existing_active: Sequence[str]) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for name in STARTER_NAMES:
        label = name
        if name in existing_active:
            label = f"{name} (already active)"
        out.append((name, label))
    return tuple(out)


def _suggestion_choices(profile: BehaviorProfile | None) -> tuple[tuple[str, str], ...]:
    if profile is None or not profile.suggestions:
        return ()
    out: list[tuple[str, str]] = []
    for i, s in enumerate(profile.suggestions, start=1):
        name = s.proposed.get("name", f"suggestion-{i}")
        out.append((str(i), f"{name}  ({s.kind}, {s.evidence}x)"))
    return tuple(out)


def build_questions(state: RepoState,
                    profile: BehaviorProfile | None) -> tuple[InterviewQuestion, ...]:
    questions: list[InterviewQuestion] = []

    # Q1: which starter toolboxes to activate?
    default_starters = "ship-it,security-sweep" if state.is_blank else ""
    questions.append(InterviewQuestion(
        id="starters",
        prompt=(
            "Which starter toolboxes should be activated? "
            "Comma-separated list or blank to skip."
        ),
        choices=_starter_choices(state.existing_active),
        multi=True,
        default=default_starters or None,
    ))

    # Q2: which mined suggestions to accept (skipped if profile empty)
    sugg = _suggestion_choices(profile)
    if sugg:
        questions.append(InterviewQuestion(
            id="suggestions",
            prompt=(
                "Accept any behaviour-miner suggestions? "
                "Comma-separated indices (1-based) or blank."
            ),
            choices=sugg,
            multi=True,
            default=None,
        ))

    # Q3: scope preference \u2014 drives scope.analysis on newly-added toolboxes
    questions.append(InterviewQuestion(
        id="analysis",
        prompt="Default analysis mode for new toolboxes?",
        choices=(
            ("dynamic", "dynamic   (diff \u2192 graph \u2192 full, recommended)"),
            ("diff", "diff      (changed files only)"),
            ("graph-blast", "graph-blast  (changed files + graph neighbours)"),
            ("full", "full      (every tracked file)"),
        ),
        multi=False,
        default="dynamic",
    ))

    return tuple(questions)


# \u2500\u2500 Answer handling \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def _parse_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _filter_known_starters(picks: Sequence[str]) -> tuple[str, ...]:
    return tuple(p for p in picks if p in STARTER_NAMES)


def _resolve_suggestion_indices(
    picks: Sequence[str],
    profile: BehaviorProfile | None,
) -> tuple[dict, ...]:
    if profile is None or not profile.suggestions:
        return ()
    out: list[dict] = []
    for raw in picks:
        try:
            idx = int(raw)
        except ValueError:
            continue
        if 1 <= idx <= len(profile.suggestions):
            sug = profile.suggestions[idx - 1]
            out.append(dict(sug.proposed))
    return tuple(out)


def compose_result(
    state: RepoState,
    profile: BehaviorProfile | None,
    answers: dict[str, str | None],
    skipped: bool = False,
) -> InterviewResult:
    if skipped:
        return InterviewResult(activated=(), accepted_suggestions=(),
                               skipped=True, notes=("user skipped interview",))

    starters = _filter_known_starters(_parse_list(answers.get("starters")))
    suggestions = _resolve_suggestion_indices(
        _parse_list(answers.get("suggestions")), profile,
    )
    notes: list[str] = []

    raw_starters = _parse_list(answers.get("starters"))
    dropped = [s for s in raw_starters if s not in STARTER_NAMES]
    if dropped:
        notes.append(f"ignored unknown starter(s): {', '.join(dropped)}")

    analysis = answers.get("analysis") or "dynamic"
    if suggestions:
        # Patch the proposed analysis mode into every accepted suggestion
        # so they honour the user's chosen default.
        suggestions = tuple(
            _apply_analysis_override(s, analysis) for s in suggestions
        )

    return InterviewResult(
        activated=starters,
        accepted_suggestions=suggestions,
        skipped=False,
        notes=tuple(notes),
    )


def _apply_analysis_override(proposed: dict, analysis: str) -> dict:
    scope = dict(proposed.get("scope", {}) or {})
    scope["analysis"] = analysis
    out = dict(proposed)
    out["scope"] = scope
    return out


# \u2500\u2500 Interactive driver \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def _render_question(q: InterviewQuestion, stream=sys.stdout) -> None:
    print(f"\n> {q.prompt}", file=stream)
    for value, label in q.choices:
        print(f"    {value}: {label}", file=stream)
    if q.default:
        print(f"  (default: {q.default})", file=stream)


def run_interactive(
    state: RepoState,
    profile: BehaviorProfile | None,
    input_fn: Callable[[str], str] = input,
    stream=None,
) -> InterviewResult:
    """
    Drive the interview via input_fn (defaults to stdin's input()).
    Typing "skip" at the first prompt aborts the whole flow.
    """
    if stream is None:
        stream = sys.stdout
    questions = build_questions(state, profile)
    answers: dict[str, str | None] = {}

    print("[toolbox] Intent interview \u2014 type 'skip' on any prompt to abort.",
          file=stream)
    print(f"  repo: {state.repo_root}", file=stream)
    print(
        f"  state: {'blank' if state.is_blank else 'populated'} "
        f"(is_git={state.is_git_repo}, commits={state.commit_count})",
        file=stream,
    )

    for q in questions:
        _render_question(q, stream=stream)
        try:
            raw = input_fn("answer> ").strip()
        except EOFError:
            raw = ""
        if raw.lower() == "skip":
            return InterviewResult(
                activated=(), accepted_suggestions=(), skipped=True,
                notes=(f"user typed 'skip' at question {q.id!r}",),
            )
        answers[q.id] = raw or q.default

    return compose_result(state, profile, answers, skipped=False)


# \u2500\u2500 Non-interactive driver \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


_PRESETS: dict[str, dict[str, str]] = {
    "blank": {
        "starters": "ship-it,security-sweep,fresh-repo-init",
        "analysis": "dynamic",
    },
    "existing": {
        "starters": "ship-it,refactor-safety",
        "analysis": "dynamic",
    },
    "docs-heavy": {
        "starters": "docs-review",
        "analysis": "diff",
    },
    "security-first": {
        "starters": "security-sweep",
        "analysis": "full",
    },
}


def run_noninteractive(
    state: RepoState,
    profile: BehaviorProfile | None,
    answers: dict[str, str | None],
) -> InterviewResult:
    return compose_result(state, profile, answers, skipped=False)


def preset_answers(preset: str) -> dict[str, str]:
    if preset not in _PRESETS:
        raise KeyError(f"Unknown preset {preset!r}; known: {sorted(_PRESETS)}")
    return dict(_PRESETS[preset])


# \u2500\u2500 Apply to ToolboxSet \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


# Where starter JSON lives \u2014 same path toolbox.py uses.
_TEMPLATES_DIR = Path(__file__).parent.parent / "docs" / "toolbox" / "templates"


def _load_template(name: str) -> dict | None:
    path = _TEMPLATES_DIR / f"{name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def apply_result(
    result: InterviewResult,
    tset: ToolboxSet | None = None,
) -> ToolboxSet:
    """
    Fold the interview result into the given ToolboxSet (or the current
    global set if not supplied). Returns the new, immutable set. Callers
    persist via save_global() at their discretion.
    """
    base = tset if tset is not None else load_global()
    if result.skipped:
        return base

    out = base
    # 1. Ensure the chosen starters are present in the set (load from
    #    template if not already there), then activate each one.
    for name in result.activated:
        if name not in out.toolboxes:
            raw = _load_template(name)
            if raw is None:
                continue
            out = out.with_toolbox(Toolbox.from_dict(name, raw))
        if name not in out.active:
            out = out.activate(name)

    # 2. Register and activate mined suggestions. Each accepted suggestion's
    #    `proposed` dict is turned into a Toolbox. Suggestions that lack a
    #    name are skipped defensively.
    for proposed in result.accepted_suggestions:
        name = str(proposed.get("name") or "").strip()
        if not name:
            continue
        if name in out.toolboxes:
            # Respect user's existing config; re-activate if needed.
            if name not in out.active:
                out = out.activate(name)
            continue
        body = {k: v for k, v in proposed.items() if k != "name"}
        out = out.with_toolbox(Toolbox.from_dict(name, body))
        out = out.activate(name)

    return out


# \u2500\u2500 CLI \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def _parse_accept_args(items: Sequence[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not items:
        return out
    for raw in items:
        if "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def cmd_detect(args: argparse.Namespace) -> int:
    state = detect_state(Path(args.repo) if args.repo else None)
    print(json.dumps(state.to_dict(), indent=2))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo) if args.repo else None
    state = detect_state(repo_root)
    profile = load_profile()
    if profile is None and args.mine:
        profile = build_profile(repo_root=repo_root)

    if args.skip:
        result = InterviewResult(
            activated=(), accepted_suggestions=(),
            skipped=True, notes=("--skip passed",),
        )
    elif args.non_interactive or args.preset:
        answers: dict[str, str | None] = dict(_parse_accept_args(args.accept))
        if args.preset:
            preset = preset_answers(args.preset)
            # CLI --accept values override the preset values.
            for k, v in preset.items():
                answers.setdefault(k, v)
        if args.starters is not None:
            answers["starters"] = args.starters
        if args.suggestions is not None:
            answers["suggestions"] = args.suggestions
        if args.analysis is not None:
            answers["analysis"] = args.analysis
        result = run_noninteractive(state, profile, answers)
    else:
        result = run_interactive(state, profile)

    payload = {
        "state": state.to_dict(),
        "result": result.to_dict(),
    }
    if args.apply and not result.skipped:
        new_set = apply_result(result)
        save_global(new_set)
        payload["applied"] = True
        payload["config_path"] = str(global_config_path())
    else:
        payload["applied"] = False

    print(json.dumps(payload, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="intent_interview")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("detect", help="Print the current RepoState as JSON")
    sp.add_argument("--repo", help="Repo root (default: cwd)")
    sp.set_defaults(func=cmd_detect)

    sp = sub.add_parser("init", help="Run the intent interview")
    sp.add_argument("--repo", help="Repo root (default: cwd)")
    sp.add_argument(
        "--non-interactive", action="store_true",
        help="Do not prompt; use --accept/--preset to supply answers.",
    )
    sp.add_argument(
        "--skip", action="store_true",
        help="Skip the interview; emit an empty result.",
    )
    sp.add_argument(
        "--preset", choices=sorted(_PRESETS),
        help="Use a canned answer set (implies --non-interactive).",
    )
    sp.add_argument(
        "--accept", nargs="*", metavar="KEY=VALUE",
        help="Structured answers in key=value form.",
    )
    sp.add_argument("--starters", help="Comma-separated starter names.")
    sp.add_argument("--suggestions", help="Comma-separated 1-based indices.")
    sp.add_argument(
        "--analysis", choices=("dynamic", "diff", "graph-blast", "full"),
        help="Default analysis mode for new toolboxes.",
    )
    sp.add_argument(
        "--mine", action="store_true",
        help="Mine behaviour first if no user profile exists on disk.",
    )
    sp.add_argument(
        "--apply", action="store_true",
        help="Persist the resulting ToolboxSet to the global config.",
    )
    sp.set_defaults(func=cmd_init)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
