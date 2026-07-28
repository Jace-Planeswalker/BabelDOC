"""Prepared-document adapter for an external publication translation runtime."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from babeldoc import __version__ as babeldoc_version
from babeldoc.format.pdf.document_il import Document
from babeldoc.format.pdf.document_il import PdfParagraph
from babeldoc.format.pdf.document_il import PdfParagraphComposition
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
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    canonical_json,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import digest
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    normalize_text,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    require_sha256,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    sha256_bytes,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    validate_approved_translations,
)
from babeldoc.format.pdf.document_il.midend.il_translator import FormulaPlaceholder
from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
from babeldoc.format.pdf.document_il.midend.il_translator import RichTextPlaceholder
from babeldoc.format.pdf.document_il.utils.paragraph_helper import (
    is_placeholder_only_paragraph,
)
from babeldoc.format.pdf.document_il.utils.paragraph_helper import (
    is_pure_numeric_paragraph,
)
from babeldoc.format.pdf.document_il.xml_converter import XMLConverter
from babeldoc.format.pdf.translation_config import TranslationConfig

_COMPOSITION_FIELDS = (
    "pdf_line",
    "pdf_formula",
    "pdf_same_style_characters",
    "pdf_character",
    "pdf_same_style_unicode_characters",
)


def _sha256_file(path: str | Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def canonical_prepared_pdf_sha256(
    path: str | Path,
    *,
    original_pdf_sha256: str,
    part_key: str,
) -> str:
    """Hash a canonical save with a deterministic trailer ID.

    PyMuPDF refreshes the second trailer ID on ordinary saves. Hashing those raw
    bytes makes identical parser inputs look different on every run. This
    in-memory canonical save removes that volatility while keeping the complete
    normalized PDF object graph in the fingerprint.
    """
    require_sha256("original_pdf_sha256", original_pdf_sha256)
    if not part_key.strip():
        raise DocumentTranslationContractError("part_key must not be empty")
    stable_id = hashlib.sha256(
        (
            "pubtrans.normalized-parser-input/v2\0"
            f"{original_pdf_sha256}\0{part_key}"
        ).encode()
    ).hexdigest()[:32].upper()
    try:
        document = pymupdf.open(path)
        document.xref_set_key(
            -1,
            "ID",
            f"[<{stable_id}><{stable_id}>]",
        )
        canonical = document.tobytes(
            garbage=4,
            clean=True,
            deflate=True,
            no_new_id=True,
        )
    except Exception as exc:
        raise DocumentTranslationContractError(
            "normalized parser-input PDF cannot be canonicalized"
        ) from exc
    finally:
        if "document" in locals():
            document.close()
    return sha256_bytes(canonical)


class ExternalPlaceholderCodec:
    """Exact, snapshot-scoped formula and rich-style placeholder codec."""

    def __init__(self, namespace: str):
        self.namespace = namespace
        # Reuse contract validation instead of accepting a near-match namespace.
        if re.fullmatch(r"PT2-[0-9a-f]{12}", namespace) is None:
            raise DocumentTranslationContractError(
                "external placeholder namespace is malformed"
            )

    @staticmethod
    def _identifier(value: int | str) -> tuple[str, bool]:
        if isinstance(value, int):
            if value < 0:
                raise DocumentTranslationContractError(
                    "placeholder id must be non-negative"
                )
            return f"{value:04d}", True
        if value != r"\d+":
            raise DocumentTranslationContractError(
                "placeholder codec only accepts integer ids or the numeric matcher"
            )
        return value, False

    def get_formular_placeholder(self, value: int | str) -> tuple[str, str]:
        identifier, exact = self._identifier(value)
        token = f"[[{self.namespace}:F:{identifier}]]"
        if exact:
            return token, re.escape(token)
        pattern = rf"\[\[{re.escape(self.namespace)}:F:\d+\]\]"
        return token, pattern

    def get_rich_text_left_placeholder(
        self,
        value: int | str,
    ) -> tuple[str, str]:
        identifier, exact = self._identifier(value)
        token = f"[[{self.namespace}:S:{identifier}:OPEN]]"
        if exact:
            return token, re.escape(token)
        pattern = rf"\[\[{re.escape(self.namespace)}:S:\d+:OPEN\]\]"
        return token, pattern

    def get_rich_text_right_placeholder(
        self,
        value: int | str,
    ) -> tuple[str, str]:
        identifier, exact = self._identifier(value)
        token = f"[[{self.namespace}:S:{identifier}:CLOSE]]"
        if exact:
            return token, re.escape(token)
        pattern = rf"\[\[{re.escape(self.namespace)}:S:\d+:CLOSE\]\]"
        return token, pattern


def choose_placeholder_namespace(
    snapshot_key: str,
    document: Document,
) -> str:
    """Choose a deterministic namespace absent from every prepared paragraph."""
    require_sha256("snapshot_key", snapshot_key)
    source = "\n".join(
        paragraph.unicode or ""
        for page in document.page
        for paragraph in page.pdf_paragraph
    )
    for attempt in range(1000):
        if attempt == 0:
            suffix = snapshot_key[:12]
        else:
            suffix = digest(
                "pubtrans.placeholder-namespace/v2",
                {"snapshot_key": snapshot_key, "attempt": attempt},
            )[:12]
        namespace = f"PT2-{suffix}"
        if f"[[{namespace}:" not in source:
            return namespace
    raise DocumentTranslationContractError(
        "unable to choose a collision-free placeholder namespace"
    )


def _layout_model_binding(
    translation_config: TranslationConfig,
    application_profile: dict[str, object],
) -> dict[str, str]:
    model = translation_config.doc_layout_model
    model_class = f"{type(model).__module__}.{type(model).__qualname__}"
    configured = application_profile.get("layout_model_sha256")
    if configured is not None:
        configured = str(configured)
        require_sha256("layout_model_sha256", configured)

    model_path_value = getattr(model, "model_path", None)
    model_path = Path(model_path_value) if model_path_value else None
    measured = None
    if model_path is not None and model_path.is_file():
        measured = _sha256_file(model_path)
    if measured is not None and configured is not None and measured != configured:
        raise DocumentTranslationContractError(
            "configured layout-model digest differs from the loaded model"
        )
    model_sha256 = measured or configured
    if model_sha256 is None:
        raise DocumentTranslationContractError(
            "external document translation requires a layout_model_sha256 "
            "when the model has no readable model_path"
        )
    return {
        "class": model_class,
        "sha256": model_sha256,
        "artifact_name": model_path.name if model_path is not None else "supplied",
    }


def build_extraction_profile(
    translation_config: TranslationConfig,
) -> dict[str, object]:
    application_profile = getattr(
        translation_config,
        "document_translation_profile",
        None,
    )
    if application_profile is None:
        application_profile = {}
    if not isinstance(application_profile, dict):
        raise DocumentTranslationContractError(
            "document_translation_profile must be a JSON object"
        )
    # Validate it before embedding it into the identity payload.
    canonical_json(application_profile)
    shared = translation_config.shared_context_cross_split_part
    profile = {
        "schema": "babeldoc.prepared-il-profile/v2",
        "application": application_profile,
        "layout_model": _layout_model_binding(
            translation_config,
            application_profile,
        ),
        "babeldoc": {
            "debug": bool(translation_config.debug),
            "pages": translation_config.pages,
            "page_ranges": translation_config.page_ranges,
            "only_include_translated_page": bool(
                translation_config.only_include_translated_page
            ),
            "skip_scanned_detection": bool(
                translation_config.skip_scanned_detection
            ),
            "ocr_workaround": bool(translation_config.ocr_workaround),
            "auto_enabled_ocr_workaround": bool(
                shared.auto_enabled_ocr_workaround
            ),
            "split_short_lines": bool(translation_config.split_short_lines),
            "short_line_split_factor": translation_config.short_line_split_factor,
            "merge_alternating_line_numbers": bool(
                translation_config.merge_alternating_line_numbers
            ),
            "formular_font_pattern": translation_config.formular_font_pattern,
            "formular_char_pattern": translation_config.formular_char_pattern,
            "remove_non_formula_lines": bool(
                translation_config.remove_non_formula_lines
            ),
            "non_formula_line_iou_threshold": (
                translation_config.non_formula_line_iou_threshold
            ),
            "figure_table_protection_threshold": (
                translation_config.figure_table_protection_threshold
            ),
            "skip_formula_offset_calculation": bool(
                translation_config.skip_formula_offset_calculation
            ),
            "enable_graphic_element_process": bool(
                translation_config.enable_graphic_element_process
            ),
            "disable_rich_text_translate": bool(
                translation_config.disable_rich_text_translate
            ),
            "primary_font_family": translation_config.primary_font_family,
            "effective_min_text_length": 0,
            "rich_text_placeholder_limit": None,
            "pymupdf_version": pymupdf.__version__,
        },
    }
    canonical_json(profile)
    return profile


def build_document_translation_context(
    translation_config: TranslationConfig,
    prepared_pdf_path: str | Path,
) -> DocumentTranslationContext:
    original_pdf_sha256 = getattr(
        translation_config,
        "document_translation_original_pdf_sha256",
        None,
    )
    if original_pdf_sha256 is None:
        raise DocumentTranslationContractError(
            "original PDF digest was not initialized before document translation"
        )
    require_sha256("original_pdf_sha256", original_pdf_sha256)
    part_key = translation_config.document_translation_part_key
    return DocumentTranslationContext.create(
        original_pdf_sha256=original_pdf_sha256,
        prepared_pdf_sha256=canonical_prepared_pdf_sha256(
            prepared_pdf_path,
            original_pdf_sha256=original_pdf_sha256,
            part_key=part_key,
        ),
        source_language=translation_config.lang_in,
        target_language=translation_config.lang_out,
        profile_name=translation_config.document_translation_profile_name,
        engine_name="BabelDOC",
        engine_version=babeldoc_version,
        engine_commit=ENGINE_COMMIT,
        extraction_profile=build_extraction_profile(translation_config),
        part_key=part_key,
    )


@dataclass(frozen=True, slots=True)
class _PreparedParagraphPlan:
    paragraph: PdfParagraph
    translate_input: ILTranslator.TranslateInput


@dataclass(frozen=True, slots=True)
class _StagedTranslation:
    paragraph: PdfParagraph
    target_text: str
    compositions: tuple[PdfParagraphComposition, ...]


class PreparedDocumentTranslator:
    """Translate a complete prepared IL through one fail-closed provider call."""

    def __init__(
        self,
        translation_config: TranslationConfig,
        prepared_pdf_path: str | Path,
    ):
        self.translation_config = translation_config
        self.prepared_pdf_path = Path(prepared_pdf_path)
        self.provider = translation_config.document_translation_provider
        if self.provider is None:
            raise DocumentTranslationContractError(
                "document translation provider is not configured"
            )
        self.converter = XMLConverter()

    def _restore_verified_artifact(
        self,
        context: DocumentTranslationContext,
        artifact: PreparedILArtifact,
    ) -> Document:
        if not isinstance(artifact, PreparedILArtifact):
            raise DocumentTranslationContractError(
                "provider returned an invalid prepared-artifact type"
            )
        if artifact.context_key != context.context_key:
            raise DocumentTranslationContractError(
                "provider returned an artifact from another document context"
            )
        try:
            document = self.converter.from_xml(artifact.xml)
            roundtrip_xml = normalize_text(self.converter.to_xml(document))
        except Exception as exc:
            raise DocumentTranslationContractError(
                "prepared IL artifact cannot be restored"
            ) from exc
        if roundtrip_xml != artifact.xml:
            raise DocumentTranslationContractError(
                "prepared IL XML is not a lossless canonical round trip"
            )
        if PreparedILArtifact.create(context, roundtrip_xml) != artifact:
            raise DocumentTranslationContractError(
                "prepared IL artifact changed during verification"
            )
        return document

    def _load_or_capture_artifact(
        self,
        context: DocumentTranslationContext,
        current: Document,
    ) -> tuple[Document, PreparedILArtifact]:
        artifact = self.provider.load_prepared_artifact(context)
        if artifact is not None:
            return self._restore_verified_artifact(context, artifact), artifact

        xml = normalize_text(self.converter.to_xml(current))
        restored = None
        for _ in range(5):
            try:
                restored = self.converter.from_xml(xml)
                canonical_xml = normalize_text(self.converter.to_xml(restored))
            except Exception as exc:
                raise DocumentTranslationContractError(
                    "current prepared IL cannot be serialized losslessly"
                ) from exc
            if canonical_xml == xml:
                break
            xml = canonical_xml
        else:
            raise DocumentTranslationContractError(
                "prepared IL XML did not reach a canonical fixed point"
            )
        assert restored is not None
        artifact = PreparedILArtifact.create(context, xml)
        restored = self._restore_verified_artifact(context, artifact)
        self.provider.save_prepared_artifact(context, artifact)
        persisted = self.provider.load_prepared_artifact(context)
        if persisted != artifact:
            raise DocumentTranslationContractError(
                "prepared artifact did not survive provider read-after-write"
            )
        return restored, artifact

    @staticmethod
    def _box(paragraph: PdfParagraph) -> BoxFingerprint | None:
        box = paragraph.box
        if box is None or any(
            value is None for value in (box.x, box.y, box.x2, box.y2)
        ):
            return None
        try:
            return BoxFingerprint.create(box.x, box.y, box.x2, box.y2)
        except DocumentTranslationContractError:
            return None

    @staticmethod
    def _composition_kinds(paragraph: PdfParagraph) -> tuple[str, ...] | None:
        kinds: list[str] = []
        for composition in paragraph.pdf_paragraph_composition:
            active = [
                field
                for field in _COMPOSITION_FIELDS
                if getattr(composition, field) is not None
            ]
            if len(active) != 1:
                return None
            kinds.append(active[0])
        return tuple(kinds)

    @staticmethod
    def _font_maps(page) -> tuple[dict, dict]:
        page_font_map = {
            font.font_id: font for font in page.pdf_font if font.font_id is not None
        }
        xobject_font_map = {}
        for xobject in page.pdf_xobject:
            if xobject.xobj_id is None:
                continue
            fonts = page_font_map.copy()
            fonts.update(
                {
                    font.font_id: font
                    for font in xobject.pdf_font
                    if font.font_id is not None
                }
            )
            xobject_font_map[xobject.xobj_id] = fonts
        return page_font_map, xobject_font_map

    @staticmethod
    def _placeholder_contract(
        namespace: str,
        translate_input: ILTranslator.TranslateInput,
    ) -> PlaceholderContract:
        specs: list[PlaceholderSpec] = []
        for placeholder in translate_input.placeholders:
            if isinstance(placeholder, FormulaPlaceholder):
                specs.append(
                    PlaceholderSpec(
                        kind="formula",
                        open_token=placeholder.placeholder,
                    )
                )
            elif isinstance(placeholder, RichTextPlaceholder):
                specs.append(
                    PlaceholderSpec(
                        kind="rich_style",
                        open_token=placeholder.left_placeholder,
                        close_token=placeholder.right_placeholder,
                    )
                )
            else:
                raise DocumentTranslationContractError(
                    "BabelDOC emitted an unknown placeholder type"
                )
        return PlaceholderContract.create(namespace, specs)

    def _record(
        self,
        *,
        snapshot: PreparedSnapshot,
        locator: UnitLocator,
        paragraph: PdfParagraph,
        disposition: str,
        reason: str,
        box: BoxFingerprint | None,
        unit: PreparedTranslationUnit | None = None,
    ) -> ParagraphRecord:
        return ParagraphRecord(
            snapshot_key=snapshot.snapshot_key,
            locator=locator,
            disposition=disposition,
            reason=reason,
            source_text=normalize_text(paragraph.unicode or ""),
            layout_label=paragraph.layout_label,
            vertical=bool(paragraph.vertical),
            box=box,
            unit=unit,
        )

    def _classify(
        self,
        *,
        snapshot: PreparedSnapshot,
        namespace: str,
        helper: ILTranslator,
        locator: UnitLocator,
        paragraph: PdfParagraph,
        page_font_map: dict,
        xobject_font_map: dict,
    ) -> tuple[ParagraphRecord, _PreparedParagraphPlan | None]:
        box = self._box(paragraph)
        if box is None:
            return (
                self._record(
                    snapshot=snapshot,
                    locator=locator,
                    paragraph=paragraph,
                    disposition="blocker",
                    reason="MISSING_GEOMETRY",
                    box=None,
                ),
                None,
            )

        source = normalize_text(paragraph.unicode or "")
        kinds = self._composition_kinds(paragraph)
        if kinds is None:
            return (
                self._record(
                    snapshot=snapshot,
                    locator=locator,
                    paragraph=paragraph,
                    disposition="blocker",
                    reason="UNKNOWN_COMPOSITION",
                    box=box,
                ),
                None,
            )
        if not kinds:
            disposition = "safe_exclusion" if not source.strip() else "blocker"
            reason = "EMPTY" if disposition == "safe_exclusion" else "UNKNOWN_COMPOSITION"
            return (
                self._record(
                    snapshot=snapshot,
                    locator=locator,
                    paragraph=paragraph,
                    disposition=disposition,
                    reason=reason,
                    box=box,
                ),
                None,
            )

        unicode_kind = "pdf_same_style_unicode_characters"
        if unicode_kind in kinds:
            all_debug = all(
                kind == unicode_kind
                and bool(
                    composition.pdf_same_style_unicode_characters.debug_info
                )
                for kind, composition in zip(
                    kinds,
                    paragraph.pdf_paragraph_composition,
                    strict=True,
                )
            )
            return (
                self._record(
                    snapshot=snapshot,
                    locator=locator,
                    paragraph=paragraph,
                    disposition="safe_exclusion" if all_debug else "blocker",
                    reason="DEBUG_ARTIFACT" if all_debug else "UNKNOWN_COMPOSITION",
                    box=box,
                ),
                None,
            )

        if not source.strip():
            return (
                self._record(
                    snapshot=snapshot,
                    locator=locator,
                    paragraph=paragraph,
                    disposition="safe_exclusion",
                    reason="EMPTY",
                    box=box,
                ),
                None,
            )
        if is_placeholder_only_paragraph(paragraph):
            return (
                self._record(
                    snapshot=snapshot,
                    locator=locator,
                    paragraph=paragraph,
                    disposition="safe_exclusion",
                    reason="FORMULA_ONLY",
                    box=box,
                ),
                None,
            )
        if is_pure_numeric_paragraph(paragraph):
            return (
                self._record(
                    snapshot=snapshot,
                    locator=locator,
                    paragraph=paragraph,
                    disposition="safe_exclusion",
                    reason="PURE_NUMERIC",
                    box=box,
                ),
                None,
            )
        if paragraph.vertical:
            return (
                self._record(
                    snapshot=snapshot,
                    locator=locator,
                    paragraph=paragraph,
                    disposition="blocker",
                    reason="VERTICAL_TEXT_UNSUPPORTED",
                    box=box,
                ),
                None,
            )
        if paragraph.pdf_style is None:
            return (
                self._record(
                    snapshot=snapshot,
                    locator=locator,
                    paragraph=paragraph,
                    disposition="blocker",
                    reason="UNKNOWN_COMPOSITION",
                    box=box,
                ),
                None,
            )

        selected_font_map = xobject_font_map.get(
            paragraph.xobj_id,
            page_font_map,
        )
        try:
            translate_input = helper.get_translate_input(
                paragraph,
                selected_font_map,
                self.translation_config.disable_rich_text_translate,
            )
        except Exception:
            translate_input = None
        if translate_input is None or not isinstance(translate_input.unicode, str):
            return (
                self._record(
                    snapshot=snapshot,
                    locator=locator,
                    paragraph=paragraph,
                    disposition="blocker",
                    reason="UNKNOWN_COMPOSITION",
                    box=box,
                ),
                None,
            )

        translate_input.unicode = normalize_text(translate_input.unicode)
        contract = self._placeholder_contract(namespace, translate_input)
        unit = PreparedTranslationUnit.create(
            snapshot=snapshot,
            locator=locator,
            source_text=translate_input.unicode,
            placeholders=contract,
            layout_label=paragraph.layout_label,
            vertical=False,
            box=box,
        )
        record = ParagraphRecord(
            snapshot_key=snapshot.snapshot_key,
            locator=locator,
            disposition="translatable",
            reason="TEXT",
            source_text=unit.source_text,
            layout_label=paragraph.layout_label,
            vertical=False,
            box=box,
            unit=unit,
        )
        return record, _PreparedParagraphPlan(paragraph, translate_input)

    @staticmethod
    def _validate_reconstructed_compositions(
        compositions: Sequence[PdfParagraphComposition],
    ) -> tuple[PdfParagraphComposition, ...]:
        result = tuple(compositions)
        if not result:
            raise DocumentTranslationContractError(
                "BabelDOC reconstructed an empty composition set"
            )
        for composition in result:
            active = [
                field
                for field in _COMPOSITION_FIELDS
                if getattr(composition, field) is not None
            ]
            if len(active) != 1:
                raise DocumentTranslationContractError(
                    "BabelDOC reconstructed an ambiguous composition"
                )
        return result

    def _stage_translations(
        self,
        helper: ILTranslator,
        document: PreparedTranslationDocument,
        plans: dict[str, _PreparedParagraphPlan],
        approvals: Sequence[ApprovedTranslation],
    ) -> tuple[_StagedTranslation, ...]:
        ordered = validate_approved_translations(document, approvals)
        staged: list[_StagedTranslation] = []
        for approval in ordered:
            self.translation_config.raise_if_cancelled()
            plan = plans.get(approval.unit_key)
            if plan is None:
                raise DocumentTranslationContractError(
                    "validated approval has no prepared paragraph plan"
                )
            try:
                compositions = helper.parse_translate_output(
                    plan.translate_input,
                    approval.target_text,
                )
                compositions = self._validate_reconstructed_compositions(
                    compositions
                )
                for composition in compositions:
                    translated = composition.pdf_same_style_unicode_characters
                    if translated is not None and translated.pdf_style is None:
                        translated.pdf_style = plan.paragraph.pdf_style
            except DocumentTranslationContractError:
                raise
            except Exception as exc:
                raise DocumentTranslationContractError(
                    f"failed to reconstruct approved unit {approval.unit_key}"
                ) from exc
            staged.append(
                _StagedTranslation(
                    paragraph=plan.paragraph,
                    target_text=approval.target_text,
                    compositions=compositions,
                )
            )
        if len(staged) != len(document.units):
            raise DocumentTranslationContractError(
                "staged translation count does not match prepared units"
            )
        return tuple(staged)

    @staticmethod
    def _apply_atomically(staged: Sequence[_StagedTranslation]) -> None:
        originals = [
            (
                item.paragraph,
                item.paragraph.unicode,
                item.paragraph.pdf_paragraph_composition,
            )
            for item in staged
        ]
        try:
            for item in staged:
                item.paragraph.unicode = item.target_text
                item.paragraph.pdf_paragraph_composition = list(item.compositions)
        except BaseException:
            for paragraph, unicode_text, compositions in originals:
                paragraph.unicode = unicode_text
                paragraph.pdf_paragraph_composition = compositions
            raise

    def translate(self, current: Document) -> Document:
        context = build_document_translation_context(
            self.translation_config,
            self.prepared_pdf_path,
        )
        prepared, artifact = self._load_or_capture_artifact(context, current)
        snapshot = PreparedSnapshot.create(context, artifact)
        namespace = choose_placeholder_namespace(snapshot.snapshot_key, prepared)
        codec = ExternalPlaceholderCodec(namespace)
        helper = ILTranslator(
            None,
            self.translation_config,
            placeholder_codec=codec,
            enable_rich_text_placeholders=(
                not self.translation_config.disable_rich_text_translate
            ),
            rich_text_placeholder_limit=None,
        )

        records: list[ParagraphRecord] = []
        plans: dict[str, _PreparedParagraphPlan] = {}
        page_counts = tuple(len(page.pdf_paragraph) for page in prepared.page)
        total_work = sum(page_counts) * 2
        with self.translation_config.progress_monitor.stage_start(
            ILTranslator.stage_name,
            total_work,
        ) as progress:
            for page_ordinal, page in enumerate(prepared.page):
                page_font_map, xobject_font_map = self._font_maps(page)
                for paragraph_ordinal, paragraph in enumerate(page.pdf_paragraph):
                    self.translation_config.raise_if_cancelled()
                    record, plan = self._classify(
                        snapshot=snapshot,
                        namespace=namespace,
                        helper=helper,
                        locator=UnitLocator(page_ordinal, paragraph_ordinal),
                        paragraph=paragraph,
                        page_font_map=page_font_map,
                        xobject_font_map=xobject_font_map,
                    )
                    records.append(record)
                    if record.unit is not None:
                        assert plan is not None
                        plans[record.unit.unit_key] = plan
                    progress.advance()

            document = PreparedTranslationDocument.create(
                snapshot=snapshot,
                page_paragraph_counts=page_counts,
                records=records,
            )
            approvals = self.provider.translate_document(document)
            if not isinstance(approvals, Sequence):
                raise DocumentTranslationContractError(
                    "document provider must return an approval sequence"
                )
            if any(not isinstance(item, ApprovedTranslation) for item in approvals):
                raise DocumentTranslationContractError(
                    "document provider returned an invalid approval envelope"
                )
            staged = self._stage_translations(
                helper,
                document,
                plans,
                approvals,
            )
            for _ in staged:
                progress.advance()
            progress.advance(sum(page_counts) - len(staged))
            self._apply_atomically(staged)
        return prepared
