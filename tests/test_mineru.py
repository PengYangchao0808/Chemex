from __future__ import annotations

import json
from pathlib import Path

import pytest

from chemex_lit.config import MinerUConfig, ModelSpec
from chemex_lit.errors import ExternalServiceError
from chemex_lit.extraction.structure import StructureExtractor
from chemex_lit.llm import LLMClient, PromptRegistry
from chemex_lit.mineru import MinerUAdapter
from chemex_lit.models import DocumentBundle, EvidenceRef


class _PromptStub(PromptRegistry):
    def __init__(self) -> None:
        pass

    def render(self, name: str, values: dict[str, str]) -> str:
        assert name == "structure"
        return values["<<IMAGE_CONTEXT>>"]


class _LLMStub(LLMClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[Path]] = []

    def complete(self, model: ModelSpec, prompt: str, images: list[Path]) -> dict[str, object]:
        self.calls.append(images)
        return {"structures": [{"label": prompt, "smiles": "CCO"}]}


def test_build_bundle_normalizes_page_idx_and_preserves_markdown_and_image_ids(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n")

    markdown_path = tmp_path / "paper.md"
    markdown_path.write_text("## Page 1\nalpha\n\n## Page 2\nbeta\n", encoding="utf-8")

    images_dir = tmp_path / "images"
    images_dir.mkdir()
    first_image = images_dir / "scheme_page1.png"
    second_image = images_dir / "scheme_p2.jpg"
    first_image.write_bytes(b"png")
    second_image.write_bytes(b"jpg")

    content_list_path = tmp_path / "paper_content_list.json"
    content_list_path.write_text(
        json.dumps(
            [
                {"type": "table", "page_idx": 0, "table_body": "t0"},
                {"type": "table", "page_idx": 3, "table_body": "t3"},
                {"type": "image", "text": "figure", "bbox": [1, 2, 3, 4]},
            ]
        ),
        encoding="utf-8",
    )

    bundle = MinerUAdapter(MinerUConfig()).build_bundle(
        pdf_path,
        markdown_path,
        images_dir,
        content_list_path,
    )

    assert [item.evidence_id for item in bundle.evidence[:2]] == ["text-p1", "text-p2"]
    assert bundle.evidence[2].evidence_id == "image-0001"
    assert bundle.evidence[3].evidence_id == "image-0002"

    layout_rows = [item for item in bundle.evidence if item.evidence_id.startswith("layout-")]
    assert [item.page for item in layout_rows] == [1, 4, None]
    assert layout_rows[2].bbox == (1.0, 2.0, 3.0, 4.0)


def test_build_bundle_sets_table_asset_path_when_img_exists(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"pdf")
    markdown_path = tmp_path / "paper.md"
    markdown_path.write_text("paper", encoding="utf-8")

    extracted_dir = tmp_path / "result"
    images_dir = extracted_dir / "images"
    nested_dir = extracted_dir / "layout"
    images_dir.mkdir(parents=True)
    nested_dir.mkdir()

    asset = images_dir / "table-1.jpg"
    asset.write_bytes(b"jpg")
    content_list_path = nested_dir / "paper_content_list.json"
    content_list_path.write_text(
        json.dumps([{"type": "table", "img_path": "images/table-1.jpg", "table_body": "row"}]),
        encoding="utf-8",
    )

    bundle = MinerUAdapter(MinerUConfig()).build_bundle(
        pdf_path,
        markdown_path,
        images_dir,
        content_list_path,
    )

    table_evidence = next(item for item in bundle.evidence if item.kind == "table")
    assert table_evidence.asset_path == str(asset.resolve())
    assert table_evidence.asset_path is not None
    assert Path(table_evidence.asset_path).is_file()


def test_build_bundle_leaves_asset_path_empty_when_img_missing(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"pdf")
    markdown_path = tmp_path / "paper.md"
    markdown_path.write_text("paper", encoding="utf-8")
    images_dir = tmp_path / "images"
    images_dir.mkdir()

    content_list_path = tmp_path / "paper_content_list.json"
    content_list_path.write_text(
        json.dumps([{"type": "table", "img_path": "images/missing.jpg", "table_body": "row"}]),
        encoding="utf-8",
    )

    bundle = MinerUAdapter(MinerUConfig()).build_bundle(
        pdf_path,
        markdown_path,
        images_dir,
        content_list_path,
    )

    table_evidence = next(item for item in bundle.evidence if item.kind == "table")
    assert table_evidence.asset_path is None


def test_select_content_list_prefers_markdown_stem_match(tmp_path: Path) -> None:
    markdown_path = tmp_path / "paper.md"
    markdown_path.write_text("paper", encoding="utf-8")
    first = tmp_path / "other_content_list.json"
    second = tmp_path / "paper_content_list.json"
    first.write_text("[]", encoding="utf-8")
    second.write_text("[]", encoding="utf-8")

    selected = MinerUAdapter._select_content_list(markdown_path, [first, second])

    assert selected == second


def test_select_content_list_raises_for_multiple_non_matching_candidates(tmp_path: Path) -> None:
    markdown_path = tmp_path / "paper.md"
    markdown_path.write_text("paper", encoding="utf-8")
    first = tmp_path / "alpha_content_list.json"
    second = tmp_path / "beta_content_list.json"
    first.write_text("[]", encoding="utf-8")
    second.write_text("[]", encoding="utf-8")

    with pytest.raises(ExternalServiceError, match="Multiple MinerU content lists found"):
        MinerUAdapter._select_content_list(markdown_path, [first, second])


def test_structure_extractor_skips_table_asset_images(tmp_path: Path) -> None:
    table_image = tmp_path / "table.jpg"
    scheme_image = tmp_path / "scheme.jpg"
    table_image.write_bytes(b"jpg")
    scheme_image.write_bytes(b"jpg")

    document = DocumentBundle(
        document_id="doc-1",
        markdown="",
        images=[str(table_image.resolve()), str(scheme_image.resolve())],
        evidence=[
            EvidenceRef(
                evidence_id="image-0001",
                kind="image",
                source_path=str(scheme_image.resolve()),
            ),
            EvidenceRef(
                evidence_id="layout-0001",
                kind="table",
                source_path=str(tmp_path / "paper_content_list.json"),
                asset_path=str(table_image.resolve()),
            ),
        ],
    )

    llm = _LLMStub()
    extractor = StructureExtractor(llm, _PromptStub(), _model_spec(), workers=1)

    rows = extractor.extract(document)

    assert len(llm.calls) == 1
    assert llm.calls[0] == [scheme_image.resolve()]
    assert len(rows) == 1


def _model_spec() -> ModelSpec:
    return ModelSpec(base_url="https://example.test", model="vision", api_key_env="VISION_KEY")
