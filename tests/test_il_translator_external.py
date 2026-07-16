import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from babeldoc.format.pdf.document_il import Box
from babeldoc.format.pdf.document_il import Document
from babeldoc.format.pdf.document_il import Page
from babeldoc.format.pdf.document_il import PdfCharacter
from babeldoc.format.pdf.document_il import PdfFont
from babeldoc.format.pdf.document_il import PdfParagraph
from babeldoc.format.pdf.document_il import PdfParagraphComposition
from babeldoc.format.pdf.document_il import PdfSameStyleCharacters
from babeldoc.format.pdf.document_il import PdfStyle
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    ApprovedTranslation,
)
from babeldoc.format.pdf.document_il.midend.il_translator_external import (
    ILTranslatorExternal,
)
from babeldoc.format.pdf.translation_config import TranslationConfig
from babeldoc.translator.translator import BaseTranslator


class DummyTranslator(BaseTranslator):
    name = "provider-test"
    model = "none"

    def __init__(self):
        super().__init__("en", "zh", ignore_cache=True)

    def do_llm_translate(self, text, rate_limit_params=None):
        raise NotImplementedError

    def do_translate(self, text, rate_limit_params=None):
        raise AssertionError("external provider must bypass text translation")


class DummyStage:
    def __init__(self):
        self.current = 0

    def advance(self, amount=1):
        self.current += amount


class DummyProgressMonitor:
    cancel_event = None

    @contextmanager
    def stage_start(self, stage_name, total):
        stage = DummyStage()
        yield stage
        if stage.current != total:
            raise AssertionError(
                f"stage {stage_name!r} advanced {stage.current}, expected {total}"
            )


class DummyFontMapper:
    def map(self, font, _text):
        return font


class RecordingProvider:
    def __init__(self, target_text):
        self.target_text = target_text
        self.units = None
        self.context = None

    def translate_document(self, units, context):
        self.units = units
        self.context = context
        return [
            ApprovedTranslation(
                unit_id=unit.unit_id,
                source_sha256=unit.source_sha256,
                placeholder_signature=unit.placeholder_signature,
                target_text=self.target_text,
            )
            for unit in units
        ]


def make_plain_paragraph(text):
    style = PdfStyle(font_id="source", font_size=10)
    return PdfParagraph(
        pdf_style=style,
        pdf_paragraph_composition=[
            PdfParagraphComposition(
                pdf_character=PdfCharacter(
                    char_unicode=text[0],
                    pdf_style=style,
                )
            )
        ],
        xobj_id=-1,
        unicode=text,
        vertical=False,
        debug_id="paragraph-1",
        layout_label="text",
    )


def make_rich_paragraph():
    base_style = PdfStyle(font_id="source", font_size=10)
    italic_style = PdfStyle(font_id="source-italic", font_size=10)

    def character(text, style, x):
        return PdfCharacter(
            char_unicode=text,
            pdf_style=style,
            box=Box(x=x, y=0, x2=x + 1, y2=1),
        )

    return PdfParagraph(
        pdf_style=base_style,
        pdf_paragraph_composition=[
            PdfParagraphComposition(pdf_character=character("A", base_style, 0)),
            PdfParagraphComposition(
                pdf_same_style_characters=PdfSameStyleCharacters(
                    pdf_style=italic_style,
                    pdf_character=[
                        character("m", italic_style, 1),
                        character("o", italic_style, 2),
                        character("i", italic_style, 3),
                    ],
                )
            ),
        ],
        xobj_id=-1,
        unicode="Amoi",
        vertical=False,
        debug_id="paragraph-rich",
        layout_label="text",
    )


class ILTranslatorExternalTests(unittest.TestCase):
    def test_document_provider_updates_short_paragraph_without_text_engine(self):
        paragraph = make_plain_paragraph("Hi")
        document = Document(page=[Page(page_number=1, pdf_paragraph=[paragraph])])
        provider = RecordingProvider("你好")

        with tempfile.TemporaryDirectory() as directory:
            source_pdf = Path(directory) / "source.pdf"
            source_pdf.write_bytes(b"provider contract fixture")
            config = TranslationConfig(
                translator=DummyTranslator(),
                input_file=source_pdf,
                lang_in="en",
                lang_out="zh",
                doc_layout_model=object(),
                working_dir=Path(directory) / "work",
                output_dir=Path(directory) / "output",
                progress_monitor=DummyProgressMonitor(),
                auto_extract_glossary=False,
                document_translation_provider=provider,
                min_text_length=5,
            )
            # Font assets are unrelated to this provider contract and are
            # normally warmed by the full PDF pipeline.
            with patch(
                "babeldoc.format.pdf.document_il.midend.il_translator.FontMapper",
                return_value=object(),
            ):
                ILTranslatorExternal(config).translate(document)

        self.assertEqual(paragraph.unicode, "你好")
        self.assertEqual(
            paragraph.pdf_paragraph_composition[
                0
            ].pdf_same_style_unicode_characters.unicode,
            "你好",
        )
        self.assertEqual(len(provider.units), 1)
        self.assertEqual(provider.units[0].source_text, "Hi")
        self.assertEqual(provider.context.lang_out, "zh")

    def test_document_provider_preserves_rich_text_without_llm_engine(self):
        paragraph = make_rich_paragraph()
        document = Document(
            page=[
                Page(
                    page_number=1,
                    pdf_font=[
                        PdfFont(name="Source", font_id="source"),
                        PdfFont(name="Source Italic", font_id="source-italic"),
                    ],
                    pdf_paragraph=[paragraph],
                )
            ]
        )
        provider = RecordingProvider("译文<b1>我</b1>")

        with tempfile.TemporaryDirectory() as directory:
            source_pdf = Path(directory) / "source.pdf"
            source_pdf.write_bytes(b"rich text provider fixture")
            config = TranslationConfig(
                translator=DummyTranslator(),
                input_file=source_pdf,
                lang_in="en",
                lang_out="zh",
                doc_layout_model=object(),
                working_dir=Path(directory) / "work",
                output_dir=Path(directory) / "output",
                progress_monitor=DummyProgressMonitor(),
                auto_extract_glossary=False,
                document_translation_provider=provider,
            )
            with patch(
                "babeldoc.format.pdf.document_il.midend.il_translator.FontMapper",
                return_value=DummyFontMapper(),
            ):
                ILTranslatorExternal(config).translate(document)

        unit = provider.units[0]
        self.assertEqual(unit.required_placeholders, ("<b1>", "</b1>"))
        self.assertEqual(unit.paired_placeholders, (("<b1>", "</b1>"),))
        styled = paragraph.pdf_paragraph_composition[1]
        self.assertEqual(
            styled.pdf_same_style_unicode_characters.unicode,
            "我",
        )
        self.assertEqual(
            styled.pdf_same_style_unicode_characters.pdf_style.font_id,
            "source-italic",
        )


if __name__ == "__main__":
    unittest.main()
