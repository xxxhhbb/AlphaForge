from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .canonical import artifact_hash, canonical_json, content_hash
from .models import (
    FailureCode,
    FrozenDocument,
    FrozenDocumentChunk,
    GatewayResult,
    PublishedAtStatus,
    ResearchMemoryError,
    SCHEMA_VERSION,
    SourceKind,
)


_PAGE_RE = re.compile(r"^\s*-{3,}\s*Page\s+(\d+)\s*-{3,}\s*$", re.I)


class FrozenDocumentStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.documents_dir = self.root / "documents"
        self.documents_dir.mkdir(parents=True, exist_ok=True)

    def freeze(self, result: GatewayResult, *, document_id: str | None = None) -> FrozenDocument:
        body_hash = content_hash(result.body)
        resolved_id = document_id or "doc_" + body_hash.removeprefix("sha256:")[:16]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", resolved_id):
            raise ResearchMemoryError(FailureCode.INVALID_SOURCE.value, "document_id contains unsafe characters")
        doc = self._build_document(result, document_id=resolved_id, body_hash=body_hash)
        doc_dir = self.documents_dir / resolved_id
        manifest_path = doc_dir / "manifest.json"
        body_path = doc_dir / "body.txt"
        if manifest_path.exists() or body_path.exists():
            existing = self.get(resolved_id)
            if existing.content_hash != doc.content_hash or existing.document_hash != doc.document_hash:
                raise ResearchMemoryError(FailureCode.DOCUMENT_COLLISION.value, "document_id already exists with different content")
            return existing
        doc_dir.mkdir(parents=True, exist_ok=False)
        _write_text_atomic(body_path, result.body)
        _write_text_atomic(manifest_path, canonical_json(doc.to_manifest()))
        return doc

    def get(self, document_id: str) -> FrozenDocument:
        doc_dir = self.documents_dir / document_id
        manifest_path = doc_dir / "manifest.json"
        body_path = doc_dir / "body.txt"
        if not manifest_path.is_file() or not body_path.is_file():
            raise ResearchMemoryError(FailureCode.DOCUMENT_NOT_FOUND.value, f"document not found: {document_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        body = body_path.read_text(encoding="utf-8")
        if manifest.get("content_hash") != content_hash(body):
            raise ResearchMemoryError(FailureCode.DOCUMENT_COLLISION.value, "stored document body hash mismatch")
        chunks = _chunk_body(document_id=document_id, body=body, body_hash=manifest["content_hash"])
        recorded = {chunk["chunk_hash"] for chunk in manifest.get("chunks", [])}
        computed = {chunk.chunk_hash for chunk in chunks}
        if recorded != computed:
            raise ResearchMemoryError(FailureCode.DOCUMENT_COLLISION.value, "stored document chunk hash mismatch")
        return FrozenDocument(
            schema_version=str(manifest.get("schema_version") or SCHEMA_VERSION),
            document_id=document_id,
            content_hash=str(manifest["content_hash"]),
            source_kind=SourceKind(str(manifest["source_kind"])),
            locator=str(manifest["locator"]),
            title=str(manifest["title"]),
            body=body,
            chunks=chunks,
            author=manifest.get("author"),
            institution=manifest.get("institution"),
            published_at=manifest.get("published_at"),
            published_at_status=PublishedAtStatus(str(manifest.get("published_at_status") or "KNOWN")),
            retrieved_at=str(manifest["retrieved_at"]),
            available_at=str(manifest["available_at"]),
            language=str(manifest.get("language") or "en"),
            license_note=str(manifest.get("license_note") or "unspecified"),
            parser_version=str(manifest.get("parser_version") or "research-memory-parser/v1"),
            source_url=manifest.get("source_url"),
            local_locator=manifest.get("local_locator"),
        )

    def has_chunk_hash(self, document_id: str, chunk_hash: str) -> bool:
        return any(chunk.chunk_hash == chunk_hash for chunk in self.get(document_id).chunks)

    def locate_chunk(self, *, document_id: str, chunk_hash: str, page: int, paragraph: int) -> FrozenDocumentChunk:
        doc = self.get(document_id)
        for chunk in doc.chunks:
            if chunk.chunk_hash == chunk_hash and chunk.page == page and chunk.paragraph == paragraph:
                return chunk
        raise ResearchMemoryError(FailureCode.INVALID_CITATION.value, "citation chunk/page/paragraph not found")

    def _build_document(self, result: GatewayResult, *, document_id: str, body_hash: str) -> FrozenDocument:
        chunks = _chunk_body(document_id=document_id, body=result.body, body_hash=body_hash)
        return FrozenDocument(
            schema_version=SCHEMA_VERSION,
            document_id=document_id,
            content_hash=body_hash,
            source_kind=result.source_kind,
            locator=result.locator,
            title=result.title,
            body=result.body,
            chunks=chunks,
            author=result.author,
            institution=result.institution,
            published_at=result.published_at,
            published_at_status=result.published_at_status,
            retrieved_at=result.retrieved_at,
            available_at=result.available_at,
            language=result.language,
            license_note=result.license_note,
            parser_version=result.parser_version,
            source_url=result.source_url,
            local_locator=result.local_locator,
        )


def _chunk_body(*, document_id: str, body: str, body_hash: str) -> tuple[FrozenDocumentChunk, ...]:
    chunks: list[FrozenDocumentChunk] = []
    page = 1
    paragraph = 0
    current_lines: list[str] = []
    current_start: int | None = None
    offset = 0

    def flush(end_offset: int) -> None:
        nonlocal paragraph, current_lines, current_start
        if current_start is None:
            return
        text = "".join(current_lines).strip()
        if text:
            paragraph += 1
            chunk_hash = artifact_hash(
                {
                    "document_id": document_id,
                    "content_hash": body_hash,
                    "page": page,
                    "paragraph": paragraph,
                    "text": text,
                }
            )
            chunks.append(
                FrozenDocumentChunk(
                    document_id=document_id,
                    chunk_index=len(chunks),
                    page=page,
                    paragraph=paragraph,
                    span_start=current_start,
                    span_end=end_offset,
                    text=text,
                    chunk_hash=chunk_hash,
                )
            )
        current_lines = []
        current_start = None

    for line in body.splitlines(keepends=True):
        line_start = offset
        offset += len(line)
        marker = _PAGE_RE.match(line.strip())
        if marker:
            flush(line_start)
            page = int(marker.group(1))
            paragraph = 0
            continue
        if not line.strip():
            flush(line_start)
            continue
        if current_start is None:
            current_start = line_start
        current_lines.append(line)
    flush(len(body))
    return tuple(chunks)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
