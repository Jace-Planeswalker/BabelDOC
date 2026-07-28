from __future__ import annotations

import dataclasses

import pytest
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    ENGINE_COMMIT,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    ApprovedTranslation,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    BoxFingerprint,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    DocumentTranslationBlockedError,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    DocumentTranslationContext,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    DocumentTranslationContractError,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    ParagraphRecord,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    PlaceholderContract,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    PlaceholderSpec,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    PreparedILArtifact,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    PreparedSnapshot,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    PreparedTranslationDocument,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    PreparedTranslationUnit,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    UnitLocator,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import digest
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    sha256_text,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    validate_approved_translations,
)


def make_context() -> DocumentTranslationContext:
    return DocumentTranslationContext.create(
        original_pdf_sha256="a" * 64,
        prepared_pdf_sha256="b" * 64,
        source_language="en",
        target_language="zh-Hans",
        profile_name="publication",
        engine_name="BabelDOC",
        engine_version="0.6.4",
        engine_commit=ENGINE_COMMIT,
        extraction_profile={"layout_model_sha256": "c" * 64},
        part_key="whole-document",
    )


def make_document(*, blocker: bool = False) -> PreparedTranslationDocument:
    context = make_context()
    artifact = PreparedILArtifact.create(context, "<document/>\n")
    snapshot = PreparedSnapshot.create(context, artifact)
    namespace = f"PT2-{snapshot.snapshot_key[:12]}"
    style = PlaceholderSpec(
        "rich_style",
        f"[[{namespace}:S:0001:OPEN]]",
        f"[[{namespace}:S:0001:CLOSE]]",
    )
    formula = PlaceholderSpec("formula", f"[[{namespace}:F:0003]]")
    placeholders = PlaceholderContract.create(namespace, (style, formula))
    source = (
        f"Hello {style.open_token}world{style.close_token} "
        f"{formula.open_token}"
    )
    box = BoxFingerprint.create(1, 2, 100, 20)
    unit = PreparedTranslationUnit.create(
        snapshot=snapshot,
        locator=UnitLocator(0, 0),
        source_text=source,
        placeholders=placeholders,
        layout_label="text",
        vertical=False,
        box=box,
    )
    records = [
        ParagraphRecord(
            snapshot_key=snapshot.snapshot_key,
            locator=unit.locator,
            disposition="translatable",
            reason="TEXT",
            source_text=source,
            layout_label="text",
            vertical=False,
            box=box,
            unit=unit,
        )
    ]
    if blocker:
        records.append(
            ParagraphRecord(
                snapshot_key=snapshot.snapshot_key,
                locator=UnitLocator(0, 1),
                disposition="blocker",
                reason="MISSING_GEOMETRY",
                source_text="Visible text",
                layout_label="text",
                vertical=False,
                box=None,
                unit=None,
            )
        )
    return PreparedTranslationDocument.create(
        snapshot=snapshot,
        page_paragraph_counts=(len(records),),
        records=records,
    )


def approve(
    unit: PreparedTranslationUnit,
    target: str | None = None,
) -> ApprovedTranslation:
    style, formula = unit.placeholders.specs
    target = target or (
        f"你好{style.open_token}世界{style.close_token}{formula.open_token}"
    )
    return ApprovedTranslation(
        approval_id=digest(
            "test.approval",
            {"unit_key": unit.unit_key, "target": target},
        ),
        unit_key=unit.unit_key,
        unit_revision=unit.unit_revision,
        target_text=target,
        target_sha256=sha256_text(target),
    )


def test_context_rejects_direct_identity_tampering() -> None:
    with pytest.raises(DocumentTranslationContractError, match="project binding"):
        dataclasses.replace(make_context(), original_pdf_sha256="d" * 64)


def test_artifact_is_bound_to_its_document_context() -> None:
    first = make_context()
    second = DocumentTranslationContext.create(
        original_pdf_sha256="d" * 64,
        prepared_pdf_sha256="b" * 64,
        source_language="en",
        target_language="zh-hans",
        profile_name="publication",
        engine_name="BabelDOC",
        engine_version="0.6.4",
        engine_commit=ENGINE_COMMIT,
        extraction_profile={"layout_model_sha256": "c" * 64},
        part_key="whole-document",
    )
    artifact = PreparedILArtifact.create(first, "<document/>\n")
    with pytest.raises(DocumentTranslationContractError, match="another"):
        PreparedSnapshot.create(second, artifact)


def test_placeholder_contract_rejects_loss_duplication_and_invention() -> None:
    unit = make_document().units[0]
    contract = unit.placeholders
    valid = approve(unit).target_text
    for invalid in (
        valid.replace(contract.tokens[0], ""),
        valid + contract.tokens[0],
        valid + f"[[{contract.namespace}:F:9999]]",
    ):
        with pytest.raises(DocumentTranslationContractError):
            contract.validate(invalid)


def test_placeholder_contract_rejects_reorder_and_empty_style() -> None:
    unit = make_document().units[0]
    style, formula = unit.placeholders.specs
    with pytest.raises(DocumentTranslationContractError, match="order"):
        unit.placeholders.validate(
            f"{formula.open_token}{style.open_token}世界{style.close_token}"
        )
    with pytest.raises(DocumentTranslationContractError, match="empty"):
        unit.placeholders.validate(
            f"{style.open_token} {style.close_token}{formula.open_token}"
        )


def test_mismatched_style_pair_ids_are_rejected() -> None:
    namespace = "PT2-123456789abc"
    with pytest.raises(DocumentTranslationContractError, match="ids do not match"):
        PlaceholderContract.create(
            namespace,
            (
                PlaceholderSpec(
                    "rich_style",
                    f"[[{namespace}:S:0001:OPEN]]",
                    f"[[{namespace}:S:0002:CLOSE]]",
                ),
            ),
        )


def test_manifest_requires_exact_paragraph_classification() -> None:
    document = make_document()
    with pytest.raises(DocumentTranslationContractError, match="every prepared"):
        PreparedTranslationDocument.create(
            snapshot=document.snapshot,
            page_paragraph_counts=(2,),
            records=document.records,
        )


def test_missing_geometry_has_no_invented_box() -> None:
    blocker = make_document(blocker=True).blockers[0]
    assert blocker.reason == "MISSING_GEOMETRY"
    assert blocker.box is None


def test_approval_set_requires_exact_current_coverage() -> None:
    document = make_document()
    unit = document.units[0]
    valid = approve(unit)
    assert validate_approved_translations(document, (valid,)) == (valid,)
    with pytest.raises(DocumentTranslationContractError, match="coverage"):
        validate_approved_translations(document, ())
    stale = dataclasses.replace(valid, unit_revision="d" * 64)
    with pytest.raises(DocumentTranslationContractError, match="stale"):
        validate_approved_translations(document, (stale,))


def test_blocker_prevents_approval_resolution() -> None:
    document = make_document(blocker=True)
    with pytest.raises(DocumentTranslationBlockedError):
        validate_approved_translations(
            document,
            tuple(approve(unit) for unit in document.units),
        )
