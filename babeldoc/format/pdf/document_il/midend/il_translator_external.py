"""Apply a document-level external provider through BabelDOC's IL machinery."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from babeldoc.format.pdf.document_il import Document
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    DocumentTranslationContext,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    DocumentTranslationContractError,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    PreparedTranslationUnit,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    make_unit_id,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    placeholder_signature,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    sha256_text,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    validate_approved_translations,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    validate_prepared_unit,
)
from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
from babeldoc.format.pdf.document_il.midend.il_translator import (
    ParagraphTranslateTracker,
)
from babeldoc.format.pdf.translation_config import TranslationConfig

logger = logging.getLogger(__name__)


class ILTranslatorExternal:
    """Prepare all units, validate all approvals, then mutate the IL once."""

    stage_name = ILTranslator.stage_name

    def __init__(self, translation_config: TranslationConfig):
        provider = translation_config.document_translation_provider
        if provider is None:
            raise ValueError("document_translation_provider is required")
        self.translation_config = translation_config
        self.provider = provider
        self.il_translator = ILTranslator(
            translation_config.translator,
            translation_config,
        )

    @staticmethod
    def _file_sha256(path: str | Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _placeholder_contract(
        translate_input,
    ) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
        tokens: list[str] = []
        pairs: list[tuple[str, str]] = []
        for placeholder in translate_input.placeholders:
            data = placeholder.to_dict()
            if token := data.get("placeholder"):
                tokens.append(token)
            left = data.get("left_placeholder")
            right = data.get("right_placeholder")
            if left and right:
                tokens.extend((left, right))
                pairs.append((left, right))
        for token, count in translate_input.original_placeholder_tokens.items():
            tokens.extend([token] * count)
        return tuple(tokens), tuple(pairs)

    def translate(self, docs: Document) -> None:
        document_sha256 = self._file_sha256(self.translation_config.input_file)
        prepared_units: list[PreparedTranslationUnit] = []
        bindings: dict[str, tuple[object, ParagraphTranslateTracker, object]] = {}
        reading_order = 0

        for page_index, page in enumerate(docs.page):
            page_number = page.page_number
            if page_number is None:
                page_number = page_index

            page_font_map = {font.font_id: font for font in page.pdf_font}
            page_xobj_font_map = {}
            for xobj in page.pdf_xobject:
                xobj_font_map = page_font_map.copy()
                for font in xobj.pdf_font:
                    xobj_font_map[font.font_id] = font
                page_xobj_font_map[xobj.xobj_id] = xobj_font_map

            for paragraph_index, paragraph in enumerate(page.pdf_paragraph):
                if paragraph.unicode is None:
                    continue
                if paragraph.vertical:
                    raise DocumentTranslationContractError(
                        "external translation does not silently skip vertical "
                        f"paragraph {paragraph.debug_id!r} on page {page_number}"
                    )

                tracker = ParagraphTranslateTracker()
                text, translate_input = self.il_translator.pre_translate_paragraph(
                    paragraph,
                    tracker,
                    page_font_map,
                    page_xobj_font_map,
                    enforce_min_text_length=False,
                    disable_rich_text_translate_override=(
                        self.translation_config.disable_rich_text_translate
                    ),
                )
                if text is None or translate_input is None:
                    logger.debug(
                        "paragraph has no translatable text: page=%s id=%s",
                        page_number,
                        paragraph.debug_id,
                    )
                    continue

                source_sha256 = sha256_text(text)
                paragraph_debug_id = paragraph.debug_id or (
                    f"paragraph-{paragraph_index}"
                )
                unit_id = make_unit_id(
                    document_sha256=document_sha256,
                    page_number=page_number,
                    paragraph_debug_id=paragraph_debug_id,
                    reading_order=reading_order,
                    source_sha256=source_sha256,
                )
                if unit_id in bindings:
                    raise DocumentTranslationContractError(
                        f"duplicate prepared unit id: {unit_id}"
                    )
                required_placeholders, paired_placeholders = self._placeholder_contract(
                    translate_input
                )
                prepared_unit = PreparedTranslationUnit(
                    unit_id=unit_id,
                    page_number=page_number,
                    paragraph_debug_id=paragraph_debug_id,
                    reading_order=reading_order,
                    source_text=text,
                    source_sha256=source_sha256,
                    required_placeholders=required_placeholders,
                    paired_placeholders=paired_placeholders,
                    placeholder_signature=placeholder_signature(
                        required_placeholders,
                        paired_placeholders,
                    ),
                    layout_label=paragraph.layout_label,
                )
                validate_prepared_unit(prepared_unit, document_sha256)
                prepared_units.append(prepared_unit)
                bindings[unit_id] = (paragraph, tracker, translate_input)
                reading_order += 1

        context = DocumentTranslationContext(
            document_sha256=document_sha256,
            lang_in=self.translation_config.lang_in,
            lang_out=self.translation_config.lang_out,
        )
        raw_approvals = list(
            self.provider.translate_document(tuple(prepared_units), context)
        )
        approval_by_id = validate_approved_translations(
            prepared_units,
            raw_approvals,
        )

        with self.translation_config.progress_monitor.stage_start(
            self.stage_name,
            len(prepared_units),
        ) as pbar:
            for unit in prepared_units:
                paragraph, tracker, translate_input = bindings[unit.unit_id]
                approval = approval_by_id[unit.unit_id]
                self.il_translator.post_translate_paragraph(
                    paragraph,
                    tracker,
                    translate_input,
                    approval.target_text,
                )
                pbar.advance(1)
