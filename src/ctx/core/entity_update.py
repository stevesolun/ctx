"""Existing entity update review helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ctx.core.wiki.wiki_utils import parse_frontmatter_and_body


@dataclass(frozen=True)
class UpdateReview:
    entity_type: str
    slug: str
    changed_fields: tuple[str, ...]
    benefits: tuple[str, ...]
    risks: tuple[str, ...]
    existing_body_lines: int
    proposed_body_lines: int
    recommendation: str

    @property
    def has_changes(self) -> bool:
        return bool(self.changed_fields or self.existing_body_lines != self.proposed_body_lines)


def _as_set(raw: Any) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {raw} if raw.strip() else set()
    if isinstance(raw, (list, tuple, set, frozenset)):
        return {str(item) for item in raw if str(item).strip()}
    return {str(raw)}


def _text(raw: Any) -> str:
    return str(raw or "").strip()


def _line_count(body: str) -> int:
    return len([line for line in body.splitlines() if line.strip()])


def _sorted_join(values: set[str]) -> str:
    return ", ".join(sorted(values))


def build_update_review(
    *,
    entity_type: str,
    slug: str,
    existing_text: str,
    proposed_text: str,
) -> UpdateReview:
    """Compare an existing entity page with proposed replacement text."""
    existing_fm, existing_body = parse_frontmatter_and_body(existing_text)
    proposed_fm, proposed_body = parse_frontmatter_and_body(proposed_text)

    changed_fields = tuple(sorted(
        key for key in set(existing_fm) | set(proposed_fm)
        if existing_fm.get(key) != proposed_fm.get(key)
    ))
    benefits: list[str] = []
    risks: list[str] = []

    for field, label in (
        ("tags", "tag"),
        ("capabilities", "capability"),
        ("setup_commands", "setup command"),
        ("verify_commands", "verify command"),
        ("model_providers", "model provider"),
        ("runtimes", "runtime"),
        ("transports", "transport"),
        ("sources", "source"),
    ):
        existing = _as_set(existing_fm.get(field))
        proposed = _as_set(proposed_fm.get(field))
        added = proposed - existing
        removed = existing - proposed
        if added:
            benefits.append(f"adds {label}(s): {_sorted_join(added)}")
        if removed:
            risks.append(f"removes {label}(s): {_sorted_join(removed)}")

    old_desc = _text(existing_fm.get("description"))
    new_desc = _text(proposed_fm.get("description"))
    if old_desc and new_desc and len(new_desc) < len(old_desc):
        risks.append("description becomes shorter")
    elif new_desc and len(new_desc) > len(old_desc):
        benefits.append("description becomes more detailed")

    old_status = _text(existing_fm.get("status"))
    new_status = _text(proposed_fm.get("status"))
    if old_status and new_status and old_status != new_status:
        risks.append(f"status changes from {old_status} to {new_status}")

    existing_lines = _line_count(existing_body)
    proposed_lines = _line_count(proposed_body)
    if proposed_lines > existing_lines:
        benefits.append(f"body gains {proposed_lines - existing_lines} line(s)")
    elif proposed_lines < existing_lines:
        risks.append(f"body loses {existing_lines - proposed_lines} line(s)")

    if not changed_fields and existing_lines == proposed_lines:
        recommendation = "skip-no-change"
    elif risks:
        recommendation = "review-before-update"
    else:
        recommendation = "apply-update"

    return UpdateReview(
        entity_type=entity_type,
        slug=slug,
        changed_fields=changed_fields,
        benefits=tuple(benefits),
        risks=tuple(risks),
        existing_body_lines=existing_lines,
        proposed_body_lines=proposed_lines,
        recommendation=recommendation,
    )


def render_update_review(review: UpdateReview) -> str:
    lines = [
        f"Existing {review.entity_type} already exists: {review.slug}",
        f"Recommendation: {review.recommendation}",
    ]
    if review.changed_fields:
        lines.append("Changed frontmatter fields: " + ", ".join(review.changed_fields))
    if review.benefits:
        lines.append("Benefits:")
        lines.extend(f"  + {item}" for item in review.benefits)
    if review.risks:
        lines.append("Risks:")
        lines.extend(f"  - {item}" for item in review.risks)
    if not review.has_changes:
        lines.append("No content or frontmatter changes detected.")
    lines.append("Use the explicit update flag to apply this replacement.")
    return "\n".join(lines)
