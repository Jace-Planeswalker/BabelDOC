from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pymupdf
import pytest
from babeldoc.docvision.base_doclayout import YoloResult
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    ApprovedTranslation,
)
from babeldoc.format.pdf.document_il.midend.document_translation_provider import digest
from babeldoc.format.pdf.document_il.midend.document_translation_provider import (
    sha256_text,
)
from babeldoc.format.pdf.high_level import translate
from babeldoc.format.pdf.translation_config import TranslationConfig
from babeldoc.format.pdf.translation_config import WatermarkOutputMode


class SyntheticLayoutModel:
    def handle_document(
        self,
        pages,
        _mupdf_document,
        _translation_config,
        _save_debug_image,
    ):
        for page in pages:
            # BabelDOC's deterministic fallback-line pass supplies the layouts.
            yield page, YoloResult(names={0: "plain text"}, boxes=[])


class IntegrationProvider:
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
        approvals = []
        for unit in document.units:
            # Prefix ordinary text while retaining every exact protected token.
            target = f"Translated {unit.source_text}"
            approvals.append(
                ApprovedTranslation(
                    approval_id=digest(
                        "integration.approval",
                        {"unit": unit.unit_key, "target": target},
                    ),
                    unit_key=unit.unit_key,
                    unit_revision=unit.unit_revision,
                    target_text=target,
                    target_sha256=sha256_text(target),
                )
            )
        return tuple(approvals)


def test_synthetic_pdf_prepare_approve_restore_and_render(tmp_path: Path) -> None:
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if not font_path.is_file():
        pytest.skip("system DejaVu Sans fixture is unavailable")

    source_path = tmp_path / "source.pdf"
    source = pymupdf.open()
    page = source.new_page(width=420, height=300)
    page.insert_text(
        (50, 70),
        "Hello publication world.",
        fontsize=14,
        fontname="helv",
    )
    page.insert_text((50, 110), "Short: Hi", fontsize=12, fontname="helv")
    page.insert_text(
        (50, 150),
        "Equation: E = mc2",
        fontsize=12,
        fontname="Times-Italic",
    )
    source.save(source_path)
    source.close()

    provider = IntegrationProvider()

    def config_for_run(run: int) -> TranslationConfig:
        return TranslationConfig(
            translator=None,
            input_file=source_path,
            lang_in="en",
            lang_out="zh-Hans",
            doc_layout_model=SyntheticLayoutModel(),
            output_dir=tmp_path / f"output-{run}",
            working_dir=tmp_path / f"work-{run}",
            no_dual=True,
            no_mono=False,
            skip_scanned_detection=True,
            watermark_output_mode=WatermarkOutputMode.NoWatermark,
            document_translation_provider=provider,
            document_translation_profile={"layout_model_sha256": "1" * 64},
        )

    def font_family(_language):
        return {
            "normal": ["test-sans"],
            "script": ["test-sans"],
            "fallback": ["test-sans"],
            "base": ["test-sans"],
        }

    def font_and_metadata(_font_name):
        return font_path, {
            "ascent": 0.928,
            "descent": -0.236,
            "encoding_length": 2,
        }

    with (
        patch("babeldoc.assets.assets.get_font_family", font_family),
        patch(
            "babeldoc.assets.assets.get_font_and_metadata",
            font_and_metadata,
        ),
        patch(
            "babeldoc.assets.assets.get_cmap_data",
            lambda _name: {"u": "", "r": [], "c": []},
        ),
    ):
        first_result = translate(config_for_run(1))
        result = translate(config_for_run(2))

    assert first_result.mono_pdf_path is not None
    assert first_result.mono_pdf_path.is_file()
    assert result.mono_pdf_path is not None
    assert result.mono_pdf_path.is_file()
    rendered = pymupdf.open(result.mono_pdf_path)
    try:
        assert rendered.page_count == 1
        output_text = "".join(page.get_text() for page in rendered)
    finally:
        rendered.close()

    assert output_text.count("Translated") == 3
    assert "Short: Hi" in output_text
    assert "E = mc2" in output_text
    # A second full parse has a fresh volatile PyMuPDF trailer ID and random
    # paragraph debug IDs, yet must restore the first canonical artifact.
    assert len(provider.artifacts) == 1
    assert len(provider.documents) == 2
    first_document, document = provider.documents
    assert first_document.snapshot.snapshot_key == document.snapshot.snapshot_key
    assert [unit.unit_key for unit in first_document.units] == [
        unit.unit_key for unit in document.units
    ]
    assert len(document.records) == 3
    assert len(document.units) == 3
    assert any(
        spec.kind == "formula"
        for unit in document.units
        for spec in unit.placeholders.specs
    )
