"""Document-level, fail-closed external translation contract.

This module intentionally has no dependency on a particular translation
runtime. A provider receives every prepared unit in one call and must return
an exact approved result set. BabelDOC remains responsible for placeholder
encoding, rich-text reconstruction, typesetting, and PDF generation.
"""

from __future__ import annotations

import hashlib
import json
import re
import string
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol


class DocumentTranslationContractError(RuntimeError):
    """The external translation handoff is incomplete or stale."""


@dataclass(frozen=True, slots=True)
class PreparedTranslationUnit:
    unit_id: str
    page_number: int
    paragraph_debug_id: str
    reading_order: int
    source_text: str
    source_sha256: str
    required_placeholders: tuple[str, ...]
    paired_placeholders: tuple[tuple[str, str], ...]
    placeholder_signature: str
    layout_label: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovedTranslation:
    unit_id: str
    source_sha256: str
    placeholder_signature: str
    target_text: str


@dataclass(frozen=True, slots=True)
class DocumentTranslationContext:
    document_sha256: str
    lang_in: str
    lang_out: str


class DocumentTranslationProvider(Protocol):
    def translate_document(
        self,
        units: tuple[PreparedTranslationUnit, ...],
        context: DocumentTranslationContext,
    ) -> Iterable[ApprovedTranslation]: ...


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_sha256(name: str, value: str) -> None:
    if len(value) != 64 or any(
        character not in string.hexdigits for character in value
    ):
        raise DocumentTranslationContractError(
            f"{name} must be a 64-character hexadecimal SHA-256"
        )


def placeholder_signature(
    tokens: Iterable[str],
    pairs: Iterable[tuple[str, str]] = (),
) -> str:
    counts = Counter(tokens)
    if "" in counts:
        raise DocumentTranslationContractError(
            "protected placeholder tokens must not be empty"
        )
    normalized_pairs = tuple(pairs)
    for left, right in normalized_pairs:
        if not left or not right or left == right:
            raise DocumentTranslationContractError(
                "placeholder pairs require distinct non-empty tokens"
            )
        if counts[left] != 1 or counts[right] != 1:
            raise DocumentTranslationContractError(
                "paired placeholder tokens must each occur exactly once"
            )
    payload = json.dumps(
        {
            "counts": sorted(counts.items()),
            "pairs": sorted(normalized_pairs),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def make_unit_id(
    *,
    document_sha256: str,
    page_number: int,
    paragraph_debug_id: str,
    reading_order: int,
    source_sha256: str,
) -> str:
    if page_number < 0 or reading_order < 0:
        raise DocumentTranslationContractError(
            "page_number and reading_order must be non-negative"
        )
    _validate_sha256("document_sha256", document_sha256)
    _validate_sha256("source_sha256", source_sha256)
    if not paragraph_debug_id:
        raise DocumentTranslationContractError("paragraph identity is required")
    return (
        f"{document_sha256}:p{page_number}:"
        f"{paragraph_debug_id}:r{reading_order}:{source_sha256}"
    )


def validate_prepared_unit(
    unit: PreparedTranslationUnit,
    document_sha256: str,
) -> None:
    if unit.source_sha256 != sha256_text(unit.source_text):
        raise DocumentTranslationContractError(
            f"source hash does not match text for unit {unit.unit_id}"
        )
    expected_signature = placeholder_signature(
        unit.required_placeholders,
        unit.paired_placeholders,
    )
    if unit.placeholder_signature != expected_signature:
        raise DocumentTranslationContractError(
            f"placeholder signature is invalid for unit {unit.unit_id}"
        )
    expected_id = make_unit_id(
        document_sha256=document_sha256,
        page_number=unit.page_number,
        paragraph_debug_id=unit.paragraph_debug_id,
        reading_order=unit.reading_order,
        source_sha256=unit.source_sha256,
    )
    if unit.unit_id != expected_id:
        raise DocumentTranslationContractError(
            f"stable identity is invalid for unit {unit.unit_id}"
        )


def _assert_placeholder_contract(
    unit: PreparedTranslationUnit,
    target_text: str,
) -> None:
    expected_tokens = Counter(unit.required_placeholders)
    if expected_tokens:
        alternatives = sorted(
            expected_tokens,
            key=lambda token: (-len(token), token),
        )
        pattern = re.compile("|".join(re.escape(token) for token in alternatives))
        actual_tokens = Counter(
            match.group(0) for match in pattern.finditer(target_text)
        )
    else:
        actual_tokens = Counter()
    if actual_tokens != expected_tokens:
        raise DocumentTranslationContractError(
            f"placeholder multiset mismatch for unit {unit.unit_id}; "
            f"expected={dict(expected_tokens)}, actual={dict(actual_tokens)}"
        )

    intervals: list[tuple[int, int]] = []
    for left, right in unit.paired_placeholders:
        left_start = target_text.find(left)
        right_start = target_text.find(right)
        if left_start < 0 or right_start < left_start + len(left):
            raise DocumentTranslationContractError(
                f"invalid placeholder pair order for unit {unit.unit_id}"
            )
        if not target_text[left_start + len(left) : right_start].strip():
            raise DocumentTranslationContractError(
                f"empty protected style span for unit {unit.unit_id}"
            )
        intervals.append((left_start, right_start + len(right)))

    intervals.sort()
    for previous, current in zip(intervals, intervals[1:], strict=False):
        if current[0] < previous[1]:
            raise DocumentTranslationContractError(
                f"overlapping protected style spans for unit {unit.unit_id}"
            )


def validate_approved_translations(
    units: Iterable[PreparedTranslationUnit],
    approvals: Iterable[ApprovedTranslation],
) -> dict[str, ApprovedTranslation]:
    """Return an exact validated map or raise before mutating the IL."""
    unit_by_id: dict[str, PreparedTranslationUnit] = {}
    for unit in units:
        if unit.unit_id in unit_by_id:
            raise DocumentTranslationContractError(
                f"duplicate prepared unit id: {unit.unit_id}"
            )
        unit_by_id[unit.unit_id] = unit

    approval_by_id: dict[str, ApprovedTranslation] = {}
    for approval in approvals:
        if approval.unit_id in approval_by_id:
            raise DocumentTranslationContractError(
                f"duplicate approved unit id: {approval.unit_id}"
            )
        approval_by_id[approval.unit_id] = approval

    expected_ids = set(unit_by_id)
    actual_ids = set(approval_by_id)
    if expected_ids != actual_ids:
        raise DocumentTranslationContractError(
            "approved unit set mismatch; "
            f"missing={sorted(expected_ids - actual_ids)}, "
            f"extra={sorted(actual_ids - expected_ids)}"
        )

    for unit_id, unit in unit_by_id.items():
        approval = approval_by_id[unit_id]
        if approval.source_sha256 != unit.source_sha256:
            raise DocumentTranslationContractError(
                f"source hash mismatch for unit {unit_id}"
            )
        if approval.placeholder_signature != unit.placeholder_signature:
            raise DocumentTranslationContractError(
                f"placeholder signature mismatch for unit {unit_id}"
            )
        if not approval.target_text.strip():
            raise DocumentTranslationContractError(
                f"empty approved target for unit {unit_id}"
            )
        _assert_placeholder_contract(unit, approval.target_text)

    return approval_by_id
