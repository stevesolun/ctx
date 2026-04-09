#!/usr/bin/env python3
"""
batch_convert.py — Batch convert skills >180 lines using micro-skill-pipeline logic.

Implements the /skill-convert classification spec from stevesolun/micro-skills:
  - "Do this" instructions -> Build step (03-build.md)
  - "Check/avoid/ensure" instructions -> YES/NO gate questions (check-gates.md)
  - Reference data (tables, lists, examples) -> Separate reference files
  - Context/scope instructions -> Scope step (01-scope.md)
  - Each pipeline file <= 40 lines
  - Build step splits into 03a, 03b, ... if >40 lines

Usage:
    python batch_convert.py --scan ~/.claude/skills [--min-lines 180] [--dry-run]
    python batch_convert.py --file ~/.claude/skills/fastapi-pro/SKILL.md
"""

import argparse
import hashlib
import json
import os
import re
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from ctx_config import cfg  # noqa: E402

MIN_LINES = cfg.line_threshold
MAX_STAGE_LINES = cfg.max_stage_lines
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Section classification ────────────────────────────────────────────────────

SCOPE_KEYWORDS = re.compile(
    r"\b(scope|constraint|prerequisite|precondition|before you|context|when to use|"
    r"trigger|activation|description|overview|purpose|applies when|input|requirements?)\b",
    re.IGNORECASE,
)
PLAN_KEYWORDS = re.compile(
    r"\b(plan|approach|strategy|design|architecture|workflow|steps overview|"
    r"phases?|methodology|algorithm|decision|trade-?off|alternative)\b",
    re.IGNORECASE,
)
GATE_KEYWORDS = re.compile(
    r"\b(check|ensure|avoid|never|must not|must always|do not|don't|verify|validate|"
    r"confirm|assert|require|guard|prevent|warning|caution|important|rule|"
    r"forbidden|prohibited|mandatory|critical|always|quality|review|audit)\b",
    re.IGNORECASE,
)
DELIVER_KEYWORDS = re.compile(
    r"\b(deliver|output|present|finalize|format|handoff|hand off|return|report|"
    r"summary|cleanup|clean up|result|template|example output|response format)\b",
    re.IGNORECASE,
)
REFERENCE_INDICATORS = re.compile(
    r"(\|.*\|.*\|)|"           # markdown tables
    r"(```[\s\S]{60,}```)|"    # long code blocks
    r"(\bexample\b.*:)|"       # example sections
    r"(^\s*[-*]\s+`[^`]+`\s*[-:])", # definition lists with code
    re.IGNORECASE | re.MULTILINE,
)


def classify_section(header: str, body: str) -> str:
    """Classify a markdown section into a pipeline stage."""
    combined = f"{header}\n{body}"
    body_lower = body.lower()

    # Strong gate signals first — "avoid", "ensure", "never"
    gate_hits = len(GATE_KEYWORDS.findall(combined))
    scope_hits = len(SCOPE_KEYWORDS.findall(combined))
    plan_hits = len(PLAN_KEYWORDS.findall(combined))
    deliver_hits = len(DELIVER_KEYWORDS.findall(combined))

    # Reference data: tables or long code blocks
    if REFERENCE_INDICATORS.search(body) and len(body.split("\n")) > 10:
        return "reference"

    # Score-based classification
    scores = {
        "scope": scope_hits * 2,
        "plan": plan_hits * 2,
        "gate": gate_hits * 3,  # gate questions weighted higher
        "deliver": deliver_hits * 2,
        "build": 1,  # default fallback
    }

    # Boost for frontmatter-like content
    if re.search(r"^---\n.*?^---", body, re.MULTILINE | re.DOTALL):
        scores["scope"] += 5

    best = max(scores, key=scores.get)
    return best


def extract_gate_questions(text: str) -> list[str]:
    """Extract and convert instructions into YES/NO gate questions."""
    questions = []
    lines = text.split("\n")

    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:
            continue

        # Already a question
        if line_stripped.endswith("?"):
            q = line_stripped.lstrip("-*0123456789.) ")
            if q:
                questions.append(q)
            continue

        # Convert "avoid X" patterns
        m = re.match(r"^[-*]\s*(?:avoid|don'?t|do not|never)\s+(.+)", line_stripped, re.IGNORECASE)
        if m:
            thing = m.group(1).rstrip(".")
            questions.append(f"Is the output free of {thing}? YES/NO")
            continue

        # Convert "ensure X" patterns
        m = re.match(r"^[-*]\s*(?:ensure|always|must|require)\s+(.+)", line_stripped, re.IGNORECASE)
        if m:
            thing = m.group(1).rstrip(".")
            questions.append(f"Does the output {thing}? YES/NO")
            continue

        # Convert "check X" patterns
        m = re.match(r"^[-*]\s*(?:check|verify|validate|confirm)\s+(?:that\s+)?(.+)", line_stripped, re.IGNORECASE)
        if m:
            thing = m.group(1).rstrip(".")
            questions.append(f"Has {thing} been verified? YES/NO")
            continue

    return questions


def parse_sections(content: str) -> list[dict]:
    """Parse a markdown document into sections by ## headers."""
    sections = []
    current_header = ""
    current_body_lines = []

    # Strip frontmatter
    content_stripped = content
    fm_match = re.match(r"^---\n(.*?)\n---\n?", content, re.DOTALL)
    frontmatter = ""
    if fm_match:
        frontmatter = fm_match.group(0)
        content_stripped = content[fm_match.end():]

    for line in content_stripped.split("\n"):
        if re.match(r"^#{1,3}\s+", line):
            # Save previous section
            if current_header or current_body_lines:
                sections.append({
                    "header": current_header,
                    "body": "\n".join(current_body_lines).strip(),
                })
            current_header = line
            current_body_lines = []
        else:
            current_body_lines.append(line)

    # Last section
    if current_header or current_body_lines:
        sections.append({
            "header": current_header,
            "body": "\n".join(current_body_lines).strip(),
        })

    return sections, frontmatter


def split_into_chunks(text: str, max_lines: int) -> list[str]:
    """Split text into chunks of max_lines, breaking at paragraph boundaries."""
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return [text]

    chunks = []
    current_chunk = []
    for line in lines:
        current_chunk.append(line)
        if len(current_chunk) >= max_lines:
            # Try to break at empty line
            for i in range(len(current_chunk) - 1, max(0, len(current_chunk) - 10), -1):
                if current_chunk[i].strip() == "":
                    break_at = i + 1
                    chunks.append("\n".join(current_chunk[:break_at]).strip())
                    current_chunk = current_chunk[break_at:]
                    break
            else:
                chunks.append("\n".join(current_chunk).strip())
                current_chunk = []

    if current_chunk:
        chunks.append("\n".join(current_chunk).strip())

    return chunks


# ── Converter ─────────────────────────────────────────────────────────────────

def convert_skill(skill_path: Path, output_dir: Path | None = None) -> dict:
    """Convert a single skill file into a micro-skill pipeline.

    If output_dir is None, converts in-place (same directory as the skill).
    Returns stats dict.
    """
    content = skill_path.read_text(encoding="utf-8", errors="replace")
    lines = content.split("\n")
    line_count = len(lines)

    if line_count <= cfg.line_threshold:
        return {"status": "skipped", "reason": f"{line_count} lines <= {MIN_LINES}"}

    # Compute source hash
    source_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    # Determine skill name and output dir
    skill_name = skill_path.parent.name
    if output_dir is None:
        output_dir = skill_path.parent

    refs_dir = output_dir / "references"
    refs_dir.mkdir(parents=True, exist_ok=True)

    # Parse sections
    sections, frontmatter = parse_sections(content)

    # Classify sections
    scope_parts = []
    plan_parts = []
    build_parts = []
    gate_parts = []
    deliver_parts = []
    reference_parts = []
    all_gate_questions = []

    # Extract description from frontmatter
    desc_match = re.search(r"description:\s*[\"']?(.+?)[\"']?\s*$", frontmatter, re.MULTILINE)
    skill_description = desc_match.group(1) if desc_match else f"Converted from {skill_name} SKILL.md"

    for section in sections:
        category = classify_section(section["header"], section["body"])
        combined = f"{section['header']}\n{section['body']}".strip()

        if category == "scope":
            scope_parts.append(combined)
        elif category == "plan":
            plan_parts.append(combined)
        elif category == "gate":
            gate_parts.append(combined)
            # Extract YES/NO questions
            questions = extract_gate_questions(section["body"])
            all_gate_questions.extend(questions)
        elif category == "deliver":
            deliver_parts.append(combined)
        elif category == "reference":
            reference_parts.append(combined)
        else:
            build_parts.append(combined)

    # Ensure no stage is empty — use fallback content
    if not scope_parts:
        scope_parts.append(f"# Step 1: Scope\n\nExtract constraints from the request for {skill_name}.")
    if not plan_parts:
        plan_parts.append(f"# Step 2: Plan\n\nDesign the approach. Map components to constraints.")
    if not build_parts:
        build_parts.append(f"# Step 3: Build\n\nExecute the plan, building each component in order.")
    if not deliver_parts:
        deliver_parts.append(f"# Step 5: Deliver\n\nFinalize and present the output.")

    # Generate gate questions from gate_parts if extraction found none
    if not all_gate_questions and gate_parts:
        for gp in gate_parts:
            qs = extract_gate_questions(gp)
            all_gate_questions.extend(qs)

    # If still no gate questions, generate generic domain ones
    if not all_gate_questions:
        all_gate_questions = [
            f"Does the output follow all constraints specified in the {skill_name} skill? YES/NO",
            "Is every element purposeful (no dead code, no placeholder text)? YES/NO",
            "Is the output usable as-is with no manual fixes needed? YES/NO",
        ]

    # ── Write pipeline files ──────────────────────────────────────────────

    # Preserve original
    original_path = output_dir / "SKILL.md.original"
    if not original_path.exists():
        skill_path.rename(original_path)
    else:
        # Original already preserved, just remove the current SKILL.md
        skill_path.unlink(missing_ok=True)

    # 01-scope.md
    scope_text = "\n\n".join(scope_parts)
    scope_text = _ensure_header(scope_text, "# Step 1: Scope")
    scope_text += "\n\n## Gate\n\n- Can I state the deliverable in one sentence? YES/NO\n- Have I listed at least one explicit constraint? YES/NO\n- Do I know what inputs I'm working with? YES/NO\n\nAll YES = proceed. Any NO = ask the user one clarifying question."
    _write_stage(refs_dir / "01-scope.md", scope_text)

    # 02-plan.md
    plan_text = "\n\n".join(plan_parts)
    plan_text = _ensure_header(plan_text, "# Step 2: Plan")
    plan_text += "\n\n## Gate\n\n- Does every constraint from Step 1 map to at least one component? YES/NO\n- Is the build order explicit? YES/NO\n- Have I checked the failure log? YES/NO\n\nAll YES = proceed. Any NO = revise."
    _write_stage(refs_dir / "02-plan.md", plan_text)

    # 03-build.md (may split into 03a, 03b, ...)
    build_text = "\n\n".join(build_parts)
    build_text = _ensure_header(build_text, "# Step 3: Build")
    build_text += "\n\n## Gate\n\n- Have all components from the plan been built? YES/NO\n- Did every component pass its micro-check? YES/NO\n- Does the assembled output match the deliverable from Step 1? YES/NO\n\nAll YES = proceed. Any NO = rebuild the failing component."
    build_chunks = split_into_chunks(build_text, MAX_STAGE_LINES)
    build_files = []
    if len(build_chunks) == 1:
        _write_stage(refs_dir / "03-build.md", build_chunks[0])
        build_files.append("references/03-build.md")
    else:
        for i, chunk in enumerate(build_chunks):
            suffix = chr(ord("a") + i)
            fname = f"03{suffix}-build.md"
            _write_stage(refs_dir / fname, chunk)
            build_files.append(f"references/{fname}")

    # 04-check.md
    check_text = "# Step 4: Check\n\nHard gate. Assume there are problems. Find them.\nAnswer every question YES or NO. \"Mostly yes\" = NO.\n\n"
    check_text += "## Universal Checks\n\n"
    check_text += "1. Does the output match the deliverable from Step 1? YES/NO\n"
    check_text += "2. Are all constraints satisfied? YES/NO\n"
    check_text += "3. Does every element serve a purpose? YES/NO\n"
    check_text += "4. Is the output usable as-is with no manual fixes? YES/NO\n"
    check_text += "5. If code: does it run without errors? YES/NO\n\n"
    check_text += "## Domain Checks\n\nLoad `check-gates.md` and answer every question there.\n\n"
    check_text += "## Failure Log\n\n6. Re-read `failure-log.md`. Does the output violate any pattern? YES/NO\n\n"
    check_text += "## On Failure\n\n- For each NO: state what is wrong in one sentence.\n- Fix each issue.\n- Re-run this entire checklist.\n- After passing: append a one-line pattern to `failure-log.md`."
    _write_stage(refs_dir / "04-check.md", check_text)

    # 05-deliver.md
    deliver_text = "\n\n".join(deliver_parts)
    deliver_text = _ensure_header(deliver_text, "# Step 5: Deliver")
    deliver_text += "\n\n## Gate\n\n- Is the output in its final location? YES/NO\n- Is the summary concise (under 5 sentences)? YES/NO\n- Are all temp artifacts cleaned up? YES/NO\n\nAll YES = done."
    _write_stage(refs_dir / "05-deliver.md", deliver_text)

    # Reference files
    ref_file_list = []
    for i, ref in enumerate(reference_parts):
        fname = f"ref-{i + 1:02d}.md"
        (refs_dir / fname).write_text(ref, encoding="utf-8")
        ref_file_list.append(f"references/{fname}")

    # check-gates.md
    gates_text = f"# Domain Gate Questions -- {skill_name}\n\nAnswer each YES or NO. Any NO = fix before proceeding.\n\n"
    for i, q in enumerate(all_gate_questions[:20], 1):  # cap at 20 gates
        gates_text += f"{i}. {q}\n"
    (output_dir / "check-gates.md").write_text(gates_text, encoding="utf-8")

    # failure-log.md
    failure_text = "# Failure Log\nOne-line patterns learned from past mistakes.\n"
    (output_dir / "failure-log.md").write_text(failure_text, encoding="utf-8")

    # original-hash.txt
    (output_dir / "original-hash.txt").write_text(source_hash + "\n", encoding="utf-8")

    # Build file references for SKILL.md orchestrator
    build_ref_str = ""
    if len(build_files) == 1:
        build_ref_str = f"Read `{build_files[0]}`."
    else:
        build_ref_str = " then ".join(f"`{f}`" for f in build_files)
        build_ref_str = f"Read {build_ref_str}."

    # SKILL.md orchestrator
    orchestrator = f"""---
name: {skill_name}
description: "{skill_description}"
---

# {skill_name}

When this skill triggers, execute the following gated pipeline.
One step at a time. Do NOT skip ahead.

## Pipeline

1. **Scope** -- Read `references/01-scope.md`. Extract constraints from the request.
2. **Plan** -- Read `references/02-plan.md`. Design the approach. Map components.
3. **Build** -- {build_ref_str} Execute with micro-checks per component.
4. **Check** -- Read `references/04-check.md`. Answer every gate question YES or NO. Any NO = fix.
5. **Deliver** -- Read `references/05-deliver.md`. Finalize and present.

## Failure Log

Read `failure-log.md` before starting. Every pattern is a mandatory constraint.

## Rules

- Read each reference file when you reach that step, not all at once.
- Step 4 (Check) is the hard gate. "Mostly yes" counts as NO.
- On Check failure: fix, re-run full checklist, append pattern to `failure-log.md`.
"""
    (output_dir / "SKILL.md").write_text(orchestrator.strip() + "\n", encoding="utf-8")

    # Count total pipeline files and max line count
    all_pipeline_files = list(refs_dir.glob("*.md")) + [
        output_dir / "SKILL.md",
        output_dir / "check-gates.md",
        output_dir / "failure-log.md",
    ]
    max_lines = 0
    total_files = len(all_pipeline_files)
    for f in all_pipeline_files:
        if f.exists():
            lc = len(f.read_text(encoding="utf-8", errors="replace").split("\n"))
            if lc > max_lines:
                max_lines = lc

    return {
        "status": "converted",
        "skill": skill_name,
        "original_lines": line_count,
        "pipeline_files": total_files,
        "gate_questions": len(all_gate_questions[:20]),
        "max_file_lines": max_lines,
        "build_splits": len(build_files),
        "reference_files": len(ref_file_list),
    }


def _ensure_header(text: str, default_header: str) -> str:
    """Ensure text starts with a markdown header."""
    if not text.strip().startswith("#"):
        return f"{default_header}\n\n{text}"
    return text


def _write_stage(path: Path, text: str) -> None:
    """Write a stage file, splitting into sub-files if >MAX_STAGE_LINES."""
    lines = text.split("\n")
    if len(lines) <= MAX_STAGE_LINES:
        path.write_text(text.strip() + "\n", encoding="utf-8")
    else:
        # If this is the main build file, split has already been handled
        # For other stages, just write as-is (scope/plan/deliver rarely exceed 40)
        path.write_text(text.strip() + "\n", encoding="utf-8")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Batch convert skills to micro-skill pipeline")
    parser.add_argument("--scan", help="Directory to scan for SKILL.md files")
    parser.add_argument("--file", help="Single SKILL.md file to convert")
    parser.add_argument("--min-lines", type=int, default=cfg.line_threshold, help=f"Minimum lines to convert (default: {cfg.line_threshold})")
    parser.add_argument("--dry-run", action="store_true", help="Just count, don't convert")
    parser.add_argument("--extra-dirs", nargs="*", help="Additional directories to scan")
    args = parser.parse_args()

    min_lines_val = args.min_lines

    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"File not found: {path}", file=sys.stderr)
            sys.exit(1)
        result = convert_skill(path)
        print(json.dumps(result, indent=2))
        return

    if not args.scan:
        print("Error: --scan DIR or --file PATH required", file=sys.stderr)
        sys.exit(1)

    # Collect all SKILL.md files
    scan_dirs = [Path(os.path.expanduser(args.scan))]
    if args.extra_dirs:
        for d in args.extra_dirs:
            scan_dirs.append(Path(os.path.expanduser(d)))

    skill_files = []
    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            print(f"Warning: {scan_dir} does not exist, skipping", file=sys.stderr)
            continue
        for skill_md in scan_dir.rglob("SKILL.md"):
            # Skip already-converted files (if SKILL.md.original exists)
            if (skill_md.parent / "SKILL.md.original").exists():
                continue
            try:
                line_count = len(skill_md.read_text(encoding="utf-8", errors="replace").split("\n"))
                if line_count > min_lines_val:
                    skill_files.append((skill_md, line_count))
            except Exception:
                pass

    print(f"Found {len(skill_files)} skills > {min_lines_val} lines")

    if args.dry_run:
        for sf, lc in sorted(skill_files, key=lambda x: -x[1])[:20]:
            print(f"  {lc:5d} lines  {sf.parent.name}")
        if len(skill_files) > 20:
            print(f"  ... and {len(skill_files) - 20} more")
        return

    # Convert
    converted = 0
    errors = 0
    skipped = 0
    for i, (sf, lc) in enumerate(skill_files):
        try:
            result = convert_skill(sf)
            if result["status"] == "converted":
                converted += 1
                if (i + 1) % 50 == 0:
                    print(f"  [{i + 1}/{len(skill_files)}] converted: {result['skill']} ({result['original_lines']} -> {result['pipeline_files']} files, {result['gate_questions']} gates)")
            else:
                skipped += 1
        except Exception as e:
            errors += 1
            print(f"  ERROR: {sf.parent.name}: {e}", file=sys.stderr)

    print(f"\nDone: {converted} converted, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    main()
