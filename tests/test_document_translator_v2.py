from __future__ import annotations

import hashlib
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pymupdf
import pytest
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
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    DocumentTranslationBlockedError,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    PreparedILArtifact,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import digest
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    sha256_text,
)
from babeldoc.format.pdf.document_il.midend.document_translator import (
    PreparedDocumentTranslator,
)
from babeldoc.format.pdf.document_il.midend.document_translator import (
    choose_placeholder_namespace,
)
from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
from babeldoc.format.pdf.translation_config import TranslationConfig


class DummyStage:
    def __init__(self, total: int):
        self.total = total
        self.current = 0

    def advance(self, amount: int = 1) -> None:
        self.current += amount


class DummyProgressMonitor:
    cancel_event = None

    def raise_if_cancelled(self) -> None:
        return None

    @contextmanager
    def stage_start(self, _name: str, total: int):
        stage = DummyStage(total)
        try:
            yield stage
        except BaseException:
            raise
        else:
            assert stage.current == total


class DummyFontMapper:
    def map(self, font, _character):
        return font


class RecordingProvider:
    def __init__(self):
        self.artifacts = {}
        self.documents = []

    def load_prepared_artifact(self, context):
        return self.artifacts.get(context.context_key)

    def save_prepared_artifact(self, context, artifact):
        assert artifact.context_key == context.context_key
        self.artifacts[context.context_key] = artifact

    def translate_document(self, document):
        self.documents.append(document)
        if document.blockers:
            return ()
        approvals = []
        for unit in document.units:
            if unit.placeholders.specs:
                style = unit.placeholders.specs[0]
                target = (
                    f"译文{style.open_token}我{style.close_token}"
                    if style.kind == "rich_style"
                    else f"译文{style.open_token}"
                )
            else:
                target = "你好"
            approvals.append(
                ApprovedTranslation(
                    approval_id=digest(
                        "test.approval",
                        {"unit": unit.unit_key, "target": target},
                    ),
                    unit_key=unit.unit_key,
                    unit_revision=unit.unit_revision,
                    target_text=target,
                    target_sha256=sha256_text(target),
                )
            )
        return tuple(approvals)


def make_plain_paragraph(text: str, *, debug_id: str = "random") -> PdfParagraph:
    style = PdfStyle(font_id="source", font_size=10.0)
    character = PdfCharacter(
        char_unicode=text[0],
        pdf_style=style,
        box=Box(0.0, 0.0, 1.0, 1.0),
    )
    return PdfParagraph(
        box=Box(0.0, 0.0, 80.0, 12.0),
        pdf_style=style,
        pdf_paragraph_composition=[
            PdfParagraphComposition(pdf_character=character)
        ],
        xobj_id=-1,
        unicode=text,
        vertical=False,
        debug_id=debug_id,
        layout_label="text",
    )


def make_rich_paragraph() -> PdfParagraph:
    base_style = PdfStyle(font_id="source", font_size=10.0)
    italic_style = PdfStyle(font_id="source-italic", font_size=10.0)

    def character(text: str, style: PdfStyle, x: float) -> PdfCharacter:
        return PdfCharacter(
            char_unicode=text,
            pdf_style=style,
            box=Box(x, 0.0, x + 1.0, 1.0),
        )

    return PdfParagraph(
        box=Box(0.0, 0.0, 80.0, 12.0),
        pdf_style=base_style,
        pdf_paragraph_composition=[
            PdfParagraphComposition(
                pdf_character=character("A", base_style, 0.0)
            ),
            PdfParagraphComposition(
                pdf_same_style_characters=PdfSameStyleCharacters(
                    box=Box(1.0, 0.0, 4.0, 1.0),
                    pdf_style=italic_style,
                    pdf_character=[
                        character("m", italic_style, 1.0),
                        character("o", italic_style, 2.0),
                        character("i", italic_style, 3.0),
                    ],
                )
            ),
        ],
        xobj_id=-1,
        unicode="Amoi",
        vertical=False,
        debug_id="random-rich",
        layout_label="text",
    )


def make_document(*paragraphs: PdfParagraph) -> Document:
    return Document(
        page=[
            Page(
                page_number=0,
                pdf_font=[
                    PdfFont(name="Source", font_id="source"),
                    PdfFont(name="Source Italic", font_id="source-italic"),
                ],
                pdf_paragraph=list(paragraphs),
            )
        ]
    )


def make_config(
    directory: str,
    provider: RecordingProvider,
) -> tuple[TranslationConfig, Path]:
    root = Path(directory)
    original = root / "original.pdf"
    prepared = root / "prepared.pdf"
    original.write_bytes(b"original publication fixture")
    prepared_document = pymupdf.open()
    prepared_document.new_page(width=100, height=100)
    prepared_document.save(prepared)
    prepared_document.close()
    config = TranslationConfig(
        translator=None,
        input_file=original,
        lang_in="en",
        lang_out="zh-Hans",
        doc_layout_model=object(),
        working_dir=root / "work",
        output_dir=root / "output",
        progress_monitor=DummyProgressMonitor(),
        document_translation_provider=provider,
        document_translation_profile={"layout_model_sha256": "1" * 64},
        document_translation_original_pdf_sha256=hashlib.sha256(
            original.read_bytes()
        ).hexdigest(),
    )
    return config, prepared


def translate(config, prepared_path, document):
    with patch(
        "babeldoc.format.pdf.document_il.midend.il_translator.FontMapper",
        return_value=DummyFontMapper(),
    ):
        return PreparedDocumentTranslator(config, prepared_path).translate(document)


def test_short_human_text_is_translated_and_artifact_is_persisted() -> None:
    with tempfile.TemporaryDirectory() as directory:
        provider = RecordingProvider()
        config, prepared_path = make_config(directory, provider)
        result = translate(config, prepared_path, make_document(make_plain_paragraph("Hi")))

    paragraph = result.page[0].pdf_paragraph[0]
    assert paragraph.unicode == "你好"
    assert paragraph.pdf_paragraph_composition[
        0
    ].pdf_same_style_unicode_characters.unicode == "你好"
    assert len(provider.artifacts) == 1
    assert len(provider.documents[0].units) == 1


def test_rich_style_survives_without_a_model_translator() -> None:
    with tempfile.TemporaryDirectory() as directory:
        provider = RecordingProvider()
        config, prepared_path = make_config(directory, provider)
        result = translate(config, prepared_path, make_document(make_rich_paragraph()))

    unit = provider.documents[0].units[0]
    assert [spec.kind for spec in unit.placeholders.specs] == ["rich_style"]
    styled = result.page[0].pdf_paragraph[0].pdf_paragraph_composition[1]
    assert styled.pdf_same_style_unicode_characters.unicode == "我"
    assert styled.pdf_same_style_unicode_characters.pdf_style.font_id == "source-italic"


def test_resume_restores_saved_artifact_despite_new_random_debug_id() -> None:
    with tempfile.TemporaryDirectory() as directory:
        provider = RecordingProvider()
        config, prepared_path = make_config(directory, provider)
        translate(
            config,
            prepared_path,
            make_document(make_plain_paragraph("Hi", debug_id="first-random")),
        )
        translate(
            config,
            prepared_path,
            make_document(make_plain_paragraph("Hi", debug_id="second-random")),
        )

    first, second = provider.documents
    assert first.snapshot.snapshot_key == second.snapshot.snapshot_key
    assert first.units[0].unit_key == second.units[0].unit_key


def test_meaningful_vertical_text_blocks_before_writeback() -> None:
    with tempfile.TemporaryDirectory() as directory:
        provider = RecordingProvider()
        config, prepared_path = make_config(directory, provider)
        paragraph = make_plain_paragraph("Vertical")
        paragraph.vertical = True
        with pytest.raises(DocumentTranslationBlockedError):
            translate(config, prepared_path, make_document(paragraph))

    assert provider.documents[0].blockers[0].reason == "VERTICAL_TEXT_UNSUPPORTED"


def test_namespace_collision_selects_a_deterministic_alternative() -> None:
    snapshot_key = "a" * 64
    paragraph = make_plain_paragraph("[[PT2-aaaaaaaaaaaa:F:0001]] text")
    document = make_document(paragraph)
    first = choose_placeholder_namespace(snapshot_key, document)
    second = choose_placeholder_namespace(snapshot_key, document)
    assert first == second
    assert first != "PT2-aaaaaaaaaaaa"


def test_reconstruction_failure_leaves_every_paragraph_unchanged() -> None:
    with tempfile.TemporaryDirectory() as directory:
        provider = RecordingProvider()
        config, prepared_path = make_config(directory, provider)
        document = make_document(
            make_plain_paragraph("First"),
            make_plain_paragraph("Second"),
        )
        translator = PreparedDocumentTranslator(config, prepared_path)

        def bypass_capture(context, current):
            artifact = PreparedILArtifact.create(
                context,
                translator.converter.to_xml(current),
            )
            return current, artifact

        original_parse = ILTranslator.parse_translate_output
        calls = 0

        def fail_on_second(helper, translate_input, output, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic final-unit reconstruction failure")
            return original_parse(helper, translate_input, output, *args, **kwargs)

        with (
            patch.object(translator, "_load_or_capture_artifact", bypass_capture),
            patch(
                "babeldoc.format.pdf.document_il.midend.il_translator.FontMapper",
                return_value=DummyFontMapper(),
            ),
            patch.object(ILTranslator, "parse_translate_output", fail_on_second),
            pytest.raises(Exception, match="failed to reconstruct"),
        ):
            translator.translate(document)

    assert [p.unicode for p in document.page[0].pdf_paragraph] == ["First", "Second"]
