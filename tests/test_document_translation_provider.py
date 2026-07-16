import unittest

from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    ApprovedTranslation,
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

DOCUMENT_HASH = "d" * 64


def make_unit(reading_order=0):
    tokens = ("<b1>", "</b1>", "<f2>")
    pairs = (("<b1>", "</b1>"),)
    source = "<b1>Hello</b1> <f2>"
    source_hash = sha256_text(source)
    return PreparedTranslationUnit(
        unit_id=make_unit_id(
            document_sha256=DOCUMENT_HASH,
            page_number=1,
            paragraph_debug_id="paragraph-1",
            reading_order=reading_order,
            source_sha256=source_hash,
        ),
        page_number=1,
        paragraph_debug_id="paragraph-1",
        reading_order=reading_order,
        source_text=source,
        source_sha256=source_hash,
        required_placeholders=tokens,
        paired_placeholders=pairs,
        placeholder_signature=placeholder_signature(tokens, pairs),
        layout_label="text",
    )


def approve(unit, target="<b1>你好</b1> <f2>"):
    return ApprovedTranslation(
        unit_id=unit.unit_id,
        source_sha256=unit.source_sha256,
        placeholder_signature=unit.placeholder_signature,
        target_text=target,
    )


class DocumentTranslationProviderContractTests(unittest.TestCase):
    def test_exact_map_passes(self):
        unit = make_unit()
        validate_prepared_unit(unit, DOCUMENT_HASH)
        result = validate_approved_translations([unit], [approve(unit)])
        self.assertEqual(result[unit.unit_id].target_text, "<b1>你好</b1> <f2>")

    def test_missing_unit_fails(self):
        with self.assertRaises(DocumentTranslationContractError):
            validate_approved_translations([make_unit()], [])

    def test_extra_unit_fails(self):
        unit = make_unit()
        extra = make_unit(1)
        with self.assertRaises(DocumentTranslationContractError):
            validate_approved_translations(
                [unit],
                [approve(unit), approve(extra)],
            )

    def test_stale_hash_fails(self):
        unit = make_unit()
        stale = ApprovedTranslation(
            unit_id=unit.unit_id,
            source_sha256="0" * 64,
            placeholder_signature=unit.placeholder_signature,
            target_text="<b1>你好</b1> <f2>",
        )
        with self.assertRaises(DocumentTranslationContractError):
            validate_approved_translations([unit], [stale])

    def test_missing_placeholder_fails(self):
        unit = make_unit()
        with self.assertRaises(DocumentTranslationContractError):
            validate_approved_translations([unit], [approve(unit, "你好")])

    def test_duplicated_placeholder_fails(self):
        unit = make_unit()
        with self.assertRaises(DocumentTranslationContractError):
            validate_approved_translations(
                [unit],
                [approve(unit, "<b1><b1>你好</b1> <f2>")],
            )

    def test_reversed_rich_text_pair_fails(self):
        unit = make_unit()
        with self.assertRaises(DocumentTranslationContractError):
            validate_approved_translations(
                [unit],
                [approve(unit, "</b1>你好<b1> <f2>")],
            )

    def test_empty_rich_text_span_fails(self):
        unit = make_unit()
        with self.assertRaises(DocumentTranslationContractError):
            validate_approved_translations(
                [unit],
                [approve(unit, "<b1></b1> <f2>")],
            )


if __name__ == "__main__":
    unittest.main()
