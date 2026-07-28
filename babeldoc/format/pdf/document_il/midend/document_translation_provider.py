"""Fail-closed document-level contract for an external translation runtime.

The fork owns prepared-IL handoff, structural validation, and atomic write-back.
Model calls, terminology, review, state, and recovery policy remain outside
BabelDOC.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN
from decimal import Decimal
from typing import Protocol

ENGINE_COMMIT = "17480db9df92ddcb37349ce34b312335226e8ec9"
CONTRACT_VERSION = "pubtrans.prepared-document/v2"
PLACEHOLDER_NAMESPACE_RE = re.compile(r"^PT2-[0-9a-f]{12}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
POINT_QUANTUM = Decimal("0.001")

DISPOSITION_REASONS = {
    "translatable": {"TEXT"},
    "safe_exclusion": {
        "EMPTY",
        "PURE_NUMERIC",
        "FORMULA_ONLY",
        "DEBUG_ARTIFACT",
    },
    "blocker": {
        "VERTICAL_TEXT_UNSUPPORTED",
        "UNKNOWN_COMPOSITION",
        "MISSING_GEOMETRY",
    },
}


class DocumentTranslationContractError(RuntimeError):
    """The prepared artifact or approval handoff is unsafe to apply."""


class DocumentTranslationBlockedError(DocumentTranslationContractError):
    """Meaningful content cannot be handled safely by the external path."""


def normalize_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("contract text must be a string")
    return unicodedata.normalize(
        "NFC",
        value.replace("\r\n", "\n").replace("\r", "\n"),
    )


def canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest(namespace: str, payload: object) -> str:
    if not namespace.strip():
        raise ValueError("digest namespace must not be empty")
    envelope = {"namespace": namespace, "payload": payload}
    return hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require_sha256(name: str, value: str) -> None:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise DocumentTranslationContractError(
            f"{name} must be a lowercase SHA-256 digest"
        )


def quantize_point(value: int | float | str | Decimal) -> str:
    if isinstance(value, float) and not math.isfinite(value):
        raise DocumentTranslationContractError("PDF geometry must be finite")
    try:
        number = Decimal(str(value)).quantize(
            POINT_QUANTUM,
            rounding=ROUND_HALF_EVEN,
        )
    except Exception as exc:
        raise DocumentTranslationContractError(
            "PDF geometry is not numeric"
        ) from exc
    if not number.is_finite():
        raise DocumentTranslationContractError("PDF geometry must be finite")
    if number == 0:
        number = Decimal(0)
    return format(number, ".3f")


@dataclass(frozen=True, slots=True)
class DocumentTranslationContext:
    context_key: str
    project_key: str
    original_pdf_sha256: str
    prepared_pdf_sha256: str
    source_language: str
    target_language: str
    profile_name: str
    engine_name: str
    engine_version: str
    engine_commit: str
    extraction_profile_json: str
    extraction_profile_sha256: str
    part_key: str

    @classmethod
    def create(
        cls,
        *,
        original_pdf_sha256: str,
        prepared_pdf_sha256: str,
        source_language: str,
        target_language: str,
        profile_name: str,
        engine_name: str,
        engine_version: str,
        engine_commit: str,
        extraction_profile: dict[str, object],
        part_key: str,
    ) -> DocumentTranslationContext:
        source_language = source_language.strip().lower()
        target_language = target_language.strip().lower()
        profile_name = profile_name.strip()
        engine_name = engine_name.strip()
        engine_version = engine_version.strip()
        engine_commit = engine_commit.strip().lower()
        part_key = part_key.strip()
        profile_json = canonical_json(extraction_profile)
        profile_sha = digest(
            "pubtrans.extraction-profile/v2",
            json.loads(profile_json),
        )
        project_payload = {
            "original_pdf_sha256": original_pdf_sha256,
            "source_language": source_language,
            "target_language": target_language,
            "profile_name": profile_name,
        }
        project_key = digest("pubtrans.project/v2", project_payload)
        context_payload = {
            "project_key": project_key,
            "prepared_pdf_sha256": prepared_pdf_sha256,
            "engine_name": engine_name,
            "engine_version": engine_version,
            "engine_commit": engine_commit,
            "extraction_profile_sha256": profile_sha,
            "part_key": part_key,
        }
        return cls(
            context_key=digest(
                "pubtrans.document-context/v2",
                context_payload,
            ),
            project_key=project_key,
            original_pdf_sha256=original_pdf_sha256,
            prepared_pdf_sha256=prepared_pdf_sha256,
            source_language=source_language,
            target_language=target_language,
            profile_name=profile_name,
            engine_name=engine_name,
            engine_version=engine_version,
            engine_commit=engine_commit,
            extraction_profile_json=profile_json,
            extraction_profile_sha256=profile_sha,
            part_key=part_key,
        )

    def __post_init__(self) -> None:
        for name in (
            "context_key",
            "project_key",
            "original_pdf_sha256",
            "prepared_pdf_sha256",
            "extraction_profile_sha256",
        ):
            require_sha256(name, getattr(self, name))
        canonical_fields = (
            self.source_language,
            self.target_language,
            self.profile_name,
            self.engine_name,
            self.engine_version,
            self.engine_commit,
            self.part_key,
        )
        if not all(value.strip() for value in canonical_fields):
            raise DocumentTranslationContractError(
                "document context fields must not be empty"
            )
        if self.source_language != self.source_language.strip().lower():
            raise DocumentTranslationContractError(
                "source language is not canonical"
            )
        if self.target_language != self.target_language.strip().lower():
            raise DocumentTranslationContractError(
                "target language is not canonical"
            )
        if any(
            value != value.strip()
            for value in (
                self.profile_name,
                self.engine_name,
                self.engine_version,
                self.engine_commit,
                self.part_key,
            )
        ):
            raise DocumentTranslationContractError(
                "document context strings are not canonical"
            )
        if self.engine_commit != ENGINE_COMMIT:
            raise DocumentTranslationContractError(
                "document provider is running against an unpinned BabelDOC commit"
            )
        try:
            profile = json.loads(self.extraction_profile_json)
        except json.JSONDecodeError as exc:
            raise DocumentTranslationContractError(
                "extraction profile JSON is malformed"
            ) from exc
        if not isinstance(profile, dict):
            raise DocumentTranslationContractError(
                "extraction profile must be a JSON object"
            )
        if canonical_json(profile) != self.extraction_profile_json:
            raise DocumentTranslationContractError(
                "extraction profile JSON is not canonical"
            )
        expected_profile = digest("pubtrans.extraction-profile/v2", profile)
        if expected_profile != self.extraction_profile_sha256:
            raise DocumentTranslationContractError(
                "extraction profile digest mismatch"
            )
        project_payload = {
            "original_pdf_sha256": self.original_pdf_sha256,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "profile_name": self.profile_name,
        }
        if digest("pubtrans.project/v2", project_payload) != self.project_key:
            raise DocumentTranslationContractError("project binding mismatch")
        context_payload = {
            "project_key": self.project_key,
            "prepared_pdf_sha256": self.prepared_pdf_sha256,
            "engine_name": self.engine_name,
            "engine_version": self.engine_version,
            "engine_commit": self.engine_commit,
            "extraction_profile_sha256": self.extraction_profile_sha256,
            "part_key": self.part_key,
        }
        if (
            digest("pubtrans.document-context/v2", context_payload)
            != self.context_key
        ):
            raise DocumentTranslationContractError("document context mismatch")

    @property
    def extraction_profile(self) -> dict[str, object]:
        result = json.loads(self.extraction_profile_json)
        assert isinstance(result, dict)
        return result


@dataclass(frozen=True, slots=True)
class PreparedILArtifact:
    context_key: str
    sha256: str
    xml: str

    @classmethod
    def create(
        cls,
        context: DocumentTranslationContext,
        xml: str,
    ) -> PreparedILArtifact:
        xml = normalize_text(xml)
        return cls(
            context_key=context.context_key,
            sha256=sha256_text(xml),
            xml=xml,
        )

    def __post_init__(self) -> None:
        require_sha256("artifact context_key", self.context_key)
        require_sha256("artifact sha256", self.sha256)
        if self.xml != normalize_text(self.xml):
            raise DocumentTranslationContractError(
                "prepared IL artifact is not NFC/LF canonical"
            )
        if self.sha256 != sha256_text(self.xml):
            raise DocumentTranslationContractError(
                "prepared IL artifact digest mismatch"
            )


@dataclass(frozen=True, slots=True)
class PreparedSnapshot:
    snapshot_key: str
    context: DocumentTranslationContext
    artifact_sha256: str

    @classmethod
    def create(
        cls,
        context: DocumentTranslationContext,
        artifact: PreparedILArtifact,
    ) -> PreparedSnapshot:
        if artifact.context_key != context.context_key:
            raise DocumentTranslationContractError(
                "prepared artifact belongs to another document context"
            )
        payload = {
            "project_key": context.project_key,
            "prepared_pdf_sha256": context.prepared_pdf_sha256,
            "engine_name": context.engine_name,
            "engine_version": context.engine_version,
            "engine_commit": context.engine_commit,
            "extraction_profile_sha256": context.extraction_profile_sha256,
            "part_key": context.part_key,
            "artifact_sha256": artifact.sha256,
        }
        return cls(
            snapshot_key=digest("pubtrans.prepared-snapshot/v2", payload),
            context=context,
            artifact_sha256=artifact.sha256,
        )

    def __post_init__(self) -> None:
        require_sha256("snapshot_key", self.snapshot_key)
        require_sha256("artifact_sha256", self.artifact_sha256)
        payload = {
            "project_key": self.context.project_key,
            "prepared_pdf_sha256": self.context.prepared_pdf_sha256,
            "engine_name": self.context.engine_name,
            "engine_version": self.context.engine_version,
            "engine_commit": self.context.engine_commit,
            "extraction_profile_sha256": self.context.extraction_profile_sha256,
            "part_key": self.context.part_key,
            "artifact_sha256": self.artifact_sha256,
        }
        if digest("pubtrans.prepared-snapshot/v2", payload) != self.snapshot_key:
            raise DocumentTranslationContractError("snapshot binding mismatch")


@dataclass(frozen=True, order=True, slots=True)
class UnitLocator:
    page_ordinal: int
    paragraph_ordinal: int

    def __post_init__(self) -> None:
        if self.page_ordinal < 0 or self.paragraph_ordinal < 0:
            raise DocumentTranslationContractError(
                "unit locator ordinals must be non-negative"
            )

    def as_payload(self) -> dict[str, int]:
        return {
            "page_ordinal": self.page_ordinal,
            "paragraph_ordinal": self.paragraph_ordinal,
        }


@dataclass(frozen=True, slots=True)
class BoxFingerprint:
    x0: str
    y0: str
    x1: str
    y1: str

    @classmethod
    def create(
        cls,
        x0: int | float | str | Decimal,
        y0: int | float | str | Decimal,
        x1: int | float | str | Decimal,
        y1: int | float | str | Decimal,
    ) -> BoxFingerprint:
        return cls(
            x0=quantize_point(x0),
            y0=quantize_point(y0),
            x1=quantize_point(x1),
            y1=quantize_point(y1),
        )

    def __post_init__(self) -> None:
        values = (self.x0, self.y0, self.x1, self.y1)
        if tuple(quantize_point(value) for value in values) != values:
            raise DocumentTranslationContractError(
                "PDF geometry is not canonical"
            )
        if float(self.x1) < float(self.x0) or float(self.y1) < float(self.y0):
            raise DocumentTranslationContractError(
                "PDF box coordinates are inverted"
            )

    def as_payload(self) -> dict[str, str]:
        return {"x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1}


@dataclass(frozen=True, slots=True)
class PlaceholderSpec:
    kind: str
    open_token: str
    close_token: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"formula", "rich_style"}:
            raise DocumentTranslationContractError(
                f"unknown placeholder kind: {self.kind}"
            )
        if not self.open_token:
            raise DocumentTranslationContractError(
                "placeholder token must not be empty"
            )
        if self.kind == "formula" and self.close_token is not None:
            raise DocumentTranslationContractError(
                "formula placeholder cannot have a close token"
            )
        if self.kind == "rich_style" and (
            not self.close_token or self.close_token == self.open_token
        ):
            raise DocumentTranslationContractError(
                "rich style requires distinct open and close tokens"
            )

    @property
    def tokens(self) -> tuple[str, ...]:
        if self.close_token is None:
            return (self.open_token,)
        return self.open_token, self.close_token

    def as_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "open_token": self.open_token,
            "close_token": self.close_token,
        }


@dataclass(frozen=True, slots=True)
class PlaceholderContract:
    namespace: str
    specs: tuple[PlaceholderSpec, ...]
    signature: str

    @classmethod
    def create(
        cls,
        namespace: str,
        specs: Sequence[PlaceholderSpec],
    ) -> PlaceholderContract:
        specs = tuple(specs)
        payload = {
            "namespace": namespace,
            "specs": [spec.as_payload() for spec in specs],
        }
        return cls(
            namespace=namespace,
            specs=specs,
            signature=digest("pubtrans.placeholder-contract/v2", payload),
        )

    def __post_init__(self) -> None:
        require_sha256("placeholder signature", self.signature)
        if PLACEHOLDER_NAMESPACE_RE.fullmatch(self.namespace) is None:
            raise DocumentTranslationContractError(
                "placeholder namespace must be PT2- plus 12 hex digits"
            )
        if not isinstance(self.specs, tuple):
            raise DocumentTranslationContractError(
                "placeholder specs must be immutable"
            )
        if len(self.tokens) != len(set(self.tokens)):
            raise DocumentTranslationContractError(
                "generated placeholder tokens are not unique"
            )
        for spec in self.specs:
            if spec.kind == "formula":
                formula_pattern = re.compile(
                    rf"^\[\[{re.escape(self.namespace)}:F:\d{{4,}}\]\]$"
                )
                if formula_pattern.fullmatch(spec.open_token) is None:
                    raise DocumentTranslationContractError(
                        "formula token is outside its declared namespace"
                    )
                continue
            open_pattern = re.compile(
                rf"^\[\[{re.escape(self.namespace)}:S:(\d{{4,}}):OPEN\]\]$"
            )
            close_pattern = re.compile(
                rf"^\[\[{re.escape(self.namespace)}:S:(\d{{4,}}):CLOSE\]\]$"
            )
            open_match = open_pattern.fullmatch(spec.open_token)
            close_match = close_pattern.fullmatch(spec.close_token or "")
            if open_match is None or close_match is None:
                raise DocumentTranslationContractError(
                    "rich-style token is outside its declared namespace"
                )
            if open_match.group(1) != close_match.group(1):
                raise DocumentTranslationContractError(
                    "rich-style open and close ids do not match"
                )
        expected = digest(
            "pubtrans.placeholder-contract/v2",
            {
                "namespace": self.namespace,
                "specs": [spec.as_payload() for spec in self.specs],
            },
        )
        if expected != self.signature:
            raise DocumentTranslationContractError(
                "placeholder signature mismatch"
            )

    @property
    def tokens(self) -> tuple[str, ...]:
        return tuple(token for spec in self.specs for token in spec.tokens)

    def validate(self, text: str, *, require_nonempty_styles: bool = True) -> None:
        if text != normalize_text(text):
            raise DocumentTranslationContractError("text is not NFC/LF canonical")
        wrong_counts = {
            token: {"expected": 1, "actual": text.count(token)}
            for token in self.tokens
            if text.count(token) != 1
        }
        if wrong_counts:
            raise DocumentTranslationContractError(
                f"placeholder occurrence mismatch: {wrong_counts}"
            )
        prefix = f"[[{self.namespace}:"
        actual: list[str] = []
        cursor = 0
        while True:
            start = text.find(prefix, cursor)
            if start < 0:
                break
            end = text.find("]]", start + len(prefix))
            if end < 0:
                raise DocumentTranslationContractError(
                    "unterminated reserved placeholder"
                )
            actual.append(text[start : end + 2])
            cursor = end + 2
        if Counter(actual) != Counter(self.tokens):
            raise DocumentTranslationContractError(
                "invented or malformed reserved placeholder"
            )
        positions = [text.find(token) for token in self.tokens]
        if positions != sorted(positions):
            raise DocumentTranslationContractError("placeholder order changed")
        if require_nonempty_styles:
            for spec in self.specs:
                if spec.kind != "rich_style":
                    continue
                assert spec.close_token is not None
                left = text.find(spec.open_token) + len(spec.open_token)
                right = text.find(spec.close_token)
                if right < left or not text[left:right].strip():
                    raise DocumentTranslationContractError(
                        "rich-style placeholder span is empty or reversed"
                    )

    def as_payload(self) -> dict[str, object]:
        return {
            "namespace": self.namespace,
            "specs": [spec.as_payload() for spec in self.specs],
            "signature": self.signature,
        }


@dataclass(frozen=True, slots=True)
class PreparedTranslationUnit:
    unit_key: str
    unit_revision: str
    snapshot_key: str
    locator: UnitLocator
    source_text: str
    source_sha256: str
    placeholders: PlaceholderContract
    layout_label: str | None
    vertical: bool
    box: BoxFingerprint

    @classmethod
    def create(
        cls,
        *,
        snapshot: PreparedSnapshot,
        locator: UnitLocator,
        source_text: str,
        placeholders: PlaceholderContract,
        layout_label: str | None,
        vertical: bool,
        box: BoxFingerprint,
    ) -> PreparedTranslationUnit:
        source_text = normalize_text(source_text)
        placeholders.validate(source_text)
        unit_key = digest(
            "pubtrans.unit/v2",
            {
                "snapshot_key": snapshot.snapshot_key,
                "locator": locator.as_payload(),
            },
        )
        revision_payload = {
            "unit_key": unit_key,
            "source_text": source_text,
            "source_sha256": sha256_text(source_text),
            "placeholder_contract": placeholders.as_payload(),
            "layout_label": layout_label,
            "vertical": vertical,
            "box": box.as_payload(),
        }
        return cls(
            unit_key=unit_key,
            unit_revision=digest(
                "pubtrans.unit-revision/v2",
                revision_payload,
            ),
            snapshot_key=snapshot.snapshot_key,
            locator=locator,
            source_text=source_text,
            source_sha256=sha256_text(source_text),
            placeholders=placeholders,
            layout_label=layout_label,
            vertical=vertical,
            box=box,
        )

    def __post_init__(self) -> None:
        for name in (
            "unit_key",
            "unit_revision",
            "snapshot_key",
            "source_sha256",
        ):
            require_sha256(name, getattr(self, name))
        if self.source_text != normalize_text(self.source_text):
            raise DocumentTranslationContractError(
                "unit source is not NFC/LF canonical"
            )
        if self.source_sha256 != sha256_text(self.source_text):
            raise DocumentTranslationContractError("unit source digest mismatch")
        self.placeholders.validate(self.source_text)
        expected_key = digest(
            "pubtrans.unit/v2",
            {
                "snapshot_key": self.snapshot_key,
                "locator": self.locator.as_payload(),
            },
        )
        if self.unit_key != expected_key:
            raise DocumentTranslationContractError("unit key mismatch")
        revision_payload = {
            "unit_key": self.unit_key,
            "source_text": self.source_text,
            "source_sha256": self.source_sha256,
            "placeholder_contract": self.placeholders.as_payload(),
            "layout_label": self.layout_label,
            "vertical": self.vertical,
            "box": self.box.as_payload(),
        }
        if self.unit_revision != digest(
            "pubtrans.unit-revision/v2",
            revision_payload,
        ):
            raise DocumentTranslationContractError("unit revision mismatch")

    def as_payload(self) -> dict[str, object]:
        return {
            "unit_key": self.unit_key,
            "unit_revision": self.unit_revision,
            "snapshot_key": self.snapshot_key,
            "locator": self.locator.as_payload(),
            "source_text": self.source_text,
            "source_sha256": self.source_sha256,
            "placeholder_contract": self.placeholders.as_payload(),
            "layout_label": self.layout_label,
            "vertical": self.vertical,
            "box": self.box.as_payload(),
        }


@dataclass(frozen=True, slots=True)
class ParagraphRecord:
    snapshot_key: str
    locator: UnitLocator
    disposition: str
    reason: str
    source_text: str
    layout_label: str | None
    vertical: bool
    box: BoxFingerprint | None
    unit: PreparedTranslationUnit | None

    def __post_init__(self) -> None:
        require_sha256("record snapshot_key", self.snapshot_key)
        if self.source_text != normalize_text(self.source_text):
            raise DocumentTranslationContractError(
                "paragraph source is not NFC/LF canonical"
            )
        if self.disposition not in DISPOSITION_REASONS:
            raise DocumentTranslationContractError(
                f"unknown paragraph disposition: {self.disposition}"
            )
        if self.reason not in DISPOSITION_REASONS[self.disposition]:
            raise DocumentTranslationContractError(
                f"reason {self.reason} is invalid for {self.disposition}"
            )
        if self.disposition == "translatable":
            if self.unit is None:
                raise DocumentTranslationContractError(
                    "translatable paragraph requires a unit"
                )
        elif self.unit is not None:
            raise DocumentTranslationContractError(
                "excluded or blocking paragraph cannot carry a unit"
            )
        if self.unit is not None:
            if self.unit.snapshot_key != self.snapshot_key:
                raise DocumentTranslationContractError(
                    "paragraph and unit snapshots differ"
                )
            if self.unit.locator != self.locator:
                raise DocumentTranslationContractError(
                    "paragraph and unit locators differ"
                )
            if self.unit.source_text != self.source_text:
                raise DocumentTranslationContractError(
                    "paragraph and unit sources differ"
                )
            if self.unit.box != self.box:
                raise DocumentTranslationContractError(
                    "paragraph and unit geometry differs"
                )
        if self.reason == "MISSING_GEOMETRY":
            if self.box is not None:
                raise DocumentTranslationContractError(
                    "missing-geometry blocker cannot carry a box"
                )
        elif self.box is None:
            raise DocumentTranslationContractError(
                "paragraph without a box must be a missing-geometry blocker"
            )
        if self.reason == "VERTICAL_TEXT_UNSUPPORTED" and not self.vertical:
            raise DocumentTranslationContractError(
                "vertical-text blocker is not marked vertical"
            )

    def as_payload(self) -> dict[str, object]:
        return {
            "snapshot_key": self.snapshot_key,
            "locator": self.locator.as_payload(),
            "disposition": self.disposition,
            "reason": self.reason,
            "source_text": self.source_text,
            "source_sha256": sha256_text(self.source_text),
            "layout_label": self.layout_label,
            "vertical": self.vertical,
            "box": self.box.as_payload() if self.box is not None else None,
            "unit": self.unit.as_payload() if self.unit else None,
        }


@dataclass(frozen=True, slots=True)
class PreparedTranslationDocument:
    snapshot: PreparedSnapshot
    page_paragraph_counts: tuple[int, ...]
    records: tuple[ParagraphRecord, ...]
    manifest_sha256: str

    @classmethod
    def create(
        cls,
        *,
        snapshot: PreparedSnapshot,
        page_paragraph_counts: Sequence[int],
        records: Sequence[ParagraphRecord],
    ) -> PreparedTranslationDocument:
        counts = tuple(page_paragraph_counts)
        records = tuple(records)
        payload = {
            "project_key": snapshot.context.project_key,
            "snapshot_key": snapshot.snapshot_key,
            "page_paragraph_counts": list(counts),
            "records": [record.as_payload() for record in records],
        }
        return cls(
            snapshot=snapshot,
            page_paragraph_counts=counts,
            records=records,
            manifest_sha256=digest(CONTRACT_VERSION, payload),
        )

    def __post_init__(self) -> None:
        require_sha256("manifest_sha256", self.manifest_sha256)
        if not isinstance(self.page_paragraph_counts, tuple) or not isinstance(
            self.records,
            tuple,
        ):
            raise DocumentTranslationContractError(
                "prepared document collections must be immutable"
            )
        if any(count < 0 for count in self.page_paragraph_counts):
            raise DocumentTranslationContractError(
                "page paragraph count must be non-negative"
            )
        expected = [
            UnitLocator(page, paragraph)
            for page, count in enumerate(self.page_paragraph_counts)
            for paragraph in range(count)
        ]
        if [record.locator for record in self.records] != expected:
            raise DocumentTranslationContractError(
                "not every prepared paragraph has exactly one classification"
            )
        if any(
            record.snapshot_key != self.snapshot.snapshot_key
            for record in self.records
        ):
            raise DocumentTranslationContractError(
                "paragraph record belongs to another snapshot"
            )
        unit_keys = [unit.unit_key for unit in self.units]
        if len(unit_keys) != len(set(unit_keys)):
            raise DocumentTranslationContractError(
                "prepared document contains duplicate unit keys"
            )
        payload = {
            "project_key": self.snapshot.context.project_key,
            "snapshot_key": self.snapshot.snapshot_key,
            "page_paragraph_counts": list(self.page_paragraph_counts),
            "records": [record.as_payload() for record in self.records],
        }
        if digest(CONTRACT_VERSION, payload) != self.manifest_sha256:
            raise DocumentTranslationContractError(
                "prepared document manifest mismatch"
            )

    @property
    def units(self) -> tuple[PreparedTranslationUnit, ...]:
        return tuple(record.unit for record in self.records if record.unit is not None)

    @property
    def blockers(self) -> tuple[ParagraphRecord, ...]:
        return tuple(
            record for record in self.records if record.disposition == "blocker"
        )


@dataclass(frozen=True, slots=True)
class ApprovedTranslation:
    approval_id: str
    unit_key: str
    unit_revision: str
    target_text: str
    target_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "approval_id",
            "unit_key",
            "unit_revision",
            "target_sha256",
        ):
            require_sha256(name, getattr(self, name))
        if self.target_text != normalize_text(self.target_text):
            raise DocumentTranslationContractError(
                "approved target is not NFC/LF canonical"
            )
        if not self.target_text.strip():
            raise DocumentTranslationContractError(
                "approved target must not be blank"
            )
        controls = [
            character
            for character in self.target_text
            if unicodedata.category(character) == "Cc"
            and character not in "\n\t"
        ]
        if controls:
            raise DocumentTranslationContractError(
                "approved target contains disallowed control characters"
            )
        if self.target_sha256 != sha256_text(self.target_text):
            raise DocumentTranslationContractError(
                "approved target digest mismatch"
            )


class DocumentTranslationProvider(Protocol):
    """Application-owned prepared artifact and approval authority.

    ``translate_document`` must persist the manifest before resolving it. If
    blockers exist, it should record them and raise instead of starting model
    work. BabelDOC independently checks blockers and exact approval coverage.
    """

    def load_prepared_artifact(
        self,
        context: DocumentTranslationContext,
    ) -> PreparedILArtifact | None: ...

    def save_prepared_artifact(
        self,
        context: DocumentTranslationContext,
        artifact: PreparedILArtifact,
    ) -> None: ...

    def translate_document(
        self,
        document: PreparedTranslationDocument,
    ) -> Sequence[ApprovedTranslation]: ...


def validate_approved_translations(
    document: PreparedTranslationDocument,
    approvals: Sequence[ApprovedTranslation],
) -> tuple[ApprovedTranslation, ...]:
    if document.blockers:
        details = [
            {
                "locator": record.locator.as_payload(),
                "reason": record.reason,
            }
            for record in document.blockers
        ]
        raise DocumentTranslationBlockedError(
            f"prepared document has blockers: {details}"
        )
    approvals = tuple(approvals)
    approval_by_unit: dict[str, ApprovedTranslation] = {}
    approval_ids: set[str] = set()
    for approval in approvals:
        if approval.approval_id in approval_ids:
            raise DocumentTranslationContractError(
                f"duplicate approval id: {approval.approval_id}"
            )
        if approval.unit_key in approval_by_unit:
            raise DocumentTranslationContractError(
                f"duplicate approval unit: {approval.unit_key}"
            )
        approval_ids.add(approval.approval_id)
        approval_by_unit[approval.unit_key] = approval

    unit_by_key = {unit.unit_key: unit for unit in document.units}
    expected = set(unit_by_key)
    actual = set(approval_by_unit)
    if expected != actual:
        raise DocumentTranslationContractError(
            "approved translation coverage mismatch; "
            f"missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    for unit_key, unit in unit_by_key.items():
        approval = approval_by_unit[unit_key]
        if approval.unit_revision != unit.unit_revision:
            raise DocumentTranslationContractError(
                f"stale approval revision: {unit_key}"
            )
        unit.placeholders.validate(approval.target_text)
    return tuple(approval_by_unit[unit.unit_key] for unit in document.units)
