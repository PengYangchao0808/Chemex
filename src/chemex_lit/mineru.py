"""MinerU cloud adapter and conversion to the canonical DocumentBundle."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from chemex_lit.config import MinerUConfig
from chemex_lit.errors import ConfigurationError, ExternalServiceError
from chemex_lit.models import DocumentBundle, EvidenceRef


class MinerUAdapter:
    """Convert a PDF with MinerU's v4 cloud API."""

    def __init__(self, config: MinerUConfig, client: httpx.Client | None = None) -> None:
        self.config = config
        self._client = client

    def convert(self, pdf_path: Path, work_dir: Path) -> DocumentBundle:
        if not pdf_path.is_file():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")
        api_key = os.environ.get(self.config.api_key_env, "").strip()
        if not api_key:
            raise ConfigurationError(
                f"Environment variable {self.config.api_key_env} is required for MinerU"
            )

        target = work_dir / "mineru"
        target.mkdir(parents=True, exist_ok=True)
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        request_url = f"{self.config.base_url.rstrip('/')}/api/v4/file-urls/batch"
        payload = {
            "files": [{"name": pdf_path.name}],
            "model_version": self.config.backend,
            "language": self.config.language,
            "enable_formula": True,
            "enable_table": True,
        }

        body = self._request("POST", request_url, headers=headers, json=payload).json()
        if body.get("code") != 0:
            raise ExternalServiceError(f"MinerU upload request failed: {body.get('msg', body)}")
        data = body.get("data", {})
        batch_id = data.get("batch_id")
        upload_urls = data.get("file_urls", [])
        if not batch_id or not upload_urls:
            raise ExternalServiceError("MinerU did not return a batch id and upload URL")

        with pdf_path.open("rb") as handle:
            upload_response = self._request("PUT", upload_urls[0], content=handle.read())
        if upload_response.status_code not in (200, 201):
            raise ExternalServiceError(f"MinerU upload failed: HTTP {upload_response.status_code}")

        zip_url = self._poll(str(batch_id), headers)
        archive = self._request("GET", zip_url, follow_redirects=True).content
        self._safe_extract(archive, target)
        markdown_path = self._find_one(target, "*.md")
        content_lists = list(target.rglob("*content_list*.json"))
        content_list_path = self._select_content_list(markdown_path, content_lists)
        images_dir = next((path for path in target.rglob("images") if path.is_dir()), target)
        return self.build_bundle(pdf_path, markdown_path, images_dir, content_list_path)

    def build_bundle(
        self,
        pdf_path: Path,
        markdown_path: Path,
        images_dir: Path,
        content_list_path: Path | None = None,
    ) -> DocumentBundle:
        """Build a DocumentBundle from an already parsed MinerU directory."""
        markdown = markdown_path.read_text(encoding="utf-8")
        document_id = hashlib.sha256(pdf_path.read_bytes()).hexdigest()[:16]
        evidence: list[EvidenceRef] = []

        markers = list(re.finditer(r"^##\s+Page\s+(\d+)", markdown, re.MULTILINE))
        if markers:
            for index, marker in enumerate(markers):
                page = int(marker.group(1))
                end = markers[index + 1].start() if index + 1 < len(markers) else len(markdown)
                text = markdown[marker.start() : end].strip()
                evidence.append(
                    EvidenceRef(
                        evidence_id=f"text-p{page}",
                        kind="text",
                        page=page,
                        source_path=str(markdown_path),
                        text=text,
                    )
                )
        elif markdown.strip():
            evidence.append(
                EvidenceRef(
                    evidence_id="text-document",
                    kind="text",
                    source_path=str(markdown_path),
                    text=markdown,
                )
            )

        images = sorted(
            path.resolve()
            for path in images_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        )
        for index, image in enumerate(images, 1):
            page_match = re.search(r"(?:page|p)[_-]?(\d+)", image.stem, re.IGNORECASE)
            page = int(page_match.group(1)) if page_match else None
            evidence.append(
                EvidenceRef(
                    evidence_id=f"image-{index:04d}",
                    kind="image",
                    page=page,
                    source_path=str(image),
                )
            )

        if content_list_path and content_list_path.is_file():
            self._add_structured_evidence(evidence, content_list_path)

        return DocumentBundle(
            document_id=document_id,
            markdown=markdown,
            images=[str(path) for path in images],
            evidence=evidence,
        )

    def _poll(self, batch_id: str, headers: dict[str, str]) -> str:
        url = f"{self.config.base_url.rstrip('/')}/api/v4/extract-results/batch/{batch_id}"
        deadline = time.monotonic() + self.config.timeout
        while time.monotonic() < deadline:
            body = self._request("GET", url, headers=headers).json()
            if body.get("code") != 0:
                raise ExternalServiceError(f"MinerU polling failed: {body.get('msg', body)}")
            results = body.get("data", {}).get("extract_result", [])
            if results:
                result = results[0]
                if result.get("state") == "done":
                    if not result.get("full_zip_url"):
                        raise ExternalServiceError("MinerU completed without a result archive")
                    return str(result["full_zip_url"])
                if result.get("state") == "failed":
                    raise ExternalServiceError(
                        f"MinerU extraction failed: {result.get('err_msg', 'unknown error')}"
                    )
            time.sleep(self.config.poll_interval)
        raise ExternalServiceError(f"MinerU timed out after {self.config.timeout}s")

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            if self._client is not None:
                response = self._client.request(method, url, **kwargs)
            else:
                with httpx.Client(timeout=self.config.timeout) as client:
                    response = client.request(method, url, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"MinerU request failed: {exc}") from exc

    @staticmethod
    def _safe_extract(archive: bytes, target: Path) -> None:
        target = target.resolve()
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            for member in zipped.infolist():
                destination = (target / member.filename).resolve()
                try:
                    destination.relative_to(target)
                except ValueError as exc:
                    raise ExternalServiceError(
                        f"Unsafe path in MinerU archive: {member.filename}"
                    ) from exc
            zipped.extractall(target)

    @staticmethod
    def _find_one(root: Path, pattern: str) -> Path:
        matches = list(root.rglob(pattern))
        if not matches:
            raise ExternalServiceError(f"MinerU result did not contain {pattern}")
        return matches[0]

    @staticmethod
    def _select_content_list(markdown_path: Path, content_lists: list[Path]) -> Path | None:
        if not content_lists:
            return None
        if len(content_lists) == 1:
            return content_lists[0]
        stem_matches = [path for path in content_lists if markdown_path.stem in path.name]
        if len(stem_matches) == 1:
            return stem_matches[0]
        if len(stem_matches) > 1:
            candidates = ", ".join(sorted(str(path) for path in stem_matches))
            raise ExternalServiceError(f"Multiple MinerU content lists matched {markdown_path.stem}: {candidates}")
        candidates = ", ".join(sorted(str(path) for path in content_lists))
        raise ExternalServiceError(
            f"Multiple MinerU content lists found for {markdown_path.name}: {candidates}"
        )

    @staticmethod
    def _add_structured_evidence(evidence: list[EvidenceRef], path: Path) -> None:
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        rows = content if isinstance(content, list) else content.get("content_list", [])
        if not isinstance(rows, list):
            return
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get("type") not in {"table", "image"}:
                continue
            page_idx = row.get("page_idx")
            # MinerU page_idx is 0-based; normalize to the 1-based page numbering used elsewhere.
            page = page_idx + 1 if page_idx is not None else row.get("page")
            bbox = row.get("bbox")
            valid_bbox = (
                (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
                if isinstance(bbox, list) and len(bbox) == 4
                else None
            )
            asset_path = MinerUAdapter._resolve_asset_path(path, row.get("img_path"))
            evidence.append(
                EvidenceRef(
                    evidence_id=f"layout-{index:04d}",
                    kind="table" if row.get("type") == "table" else "image",
                    page=page,
                    source_path=str(path),
                    asset_path=str(asset_path) if asset_path is not None else None,
                    text=row.get("text") or row.get("table_body"),
                    bbox=valid_bbox,
                )
            )

    @staticmethod
    def _resolve_asset_path(content_list_path: Path, raw_img_path: object) -> Path | None:
        if not isinstance(raw_img_path, str) or not raw_img_path.strip():
            return None
        img_path = Path(raw_img_path)
        candidates = []
        if img_path.is_absolute():
            candidates.append(img_path)
        else:
            candidates.extend(
                [
                    content_list_path.parent / img_path,
                    content_list_path.parent.parent / img_path,
                ]
            )
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_file():
                return resolved
        return None
