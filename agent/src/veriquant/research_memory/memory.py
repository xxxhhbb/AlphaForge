from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .canonical import artifact_hash, canonical_json, parse_datetime
from .models import Citation, EvidenceGroundedHypothesis, FailureCode, MemoryRecord, ResearchMemoryError
from .security import assert_no_forbidden_memory_payload, redact_sensitive


class ResearchMemoryStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.records_path = self.root / "records.jsonl"

    def build_record(
        self,
        hypothesis: EvidenceGroundedHypothesis,
        *,
        factor_draft_hash: str,
        train_validation_evidence_summary: dict[str, Any],
        public_failure_reason: str | None,
        lineage: tuple[str, ...],
        created_at: str,
        decision_time: str,
        available_at: str,
    ) -> MemoryRecord:
        base = {
            "redacted_hypothesis": redact_sensitive(hypothesis.hypothesis),
            "citations": hypothesis.citations,
            "factor_draft_hash": factor_draft_hash,
            "train_validation_evidence_summary": train_validation_evidence_summary,
            "public_failure_reason": public_failure_reason,
            "lineage": lineage,
            "created_at": created_at,
            "decision_time": decision_time,
            "available_at": available_at,
        }
        record_id = "mem_" + artifact_hash(base).removeprefix("sha256:")[:20]
        return MemoryRecord(record_id=record_id, **base)

    def append(self, record: MemoryRecord) -> MemoryRecord:
        payload = _record_to_dict(record)
        assert_no_forbidden_memory_payload(payload)
        if record.redacted_hypothesis != redact_sensitive(record.redacted_hypothesis):
            raise ResearchMemoryError(
                FailureCode.FORBIDDEN_MEMORY_PAYLOAD.value,
                "redacted_hypothesis still contains a secret-like value",
            )
        line = canonical_json(payload)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.records_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def all_records(self) -> tuple[MemoryRecord, ...]:
        if not self.records_path.exists():
            return ()
        records: list[MemoryRecord] = []
        with self.records_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                payload = json.loads(line)
                assert_no_forbidden_memory_payload(payload)
                records.append(_record_from_dict(payload))
        return tuple(records)

    def query_as_of(self, decision_time: str) -> tuple[MemoryRecord, ...]:
        cutoff = parse_datetime(decision_time, field_name="decision_time")
        visible: list[MemoryRecord] = []
        for record in self.all_records():
            if parse_datetime(record.available_at, field_name="available_at") <= cutoff and parse_datetime(
                record.decision_time, field_name="record.decision_time"
            ) <= cutoff:
                visible.append(record)
        return tuple(visible)


def _record_to_dict(record: MemoryRecord) -> dict[str, Any]:
    payload = asdict(record)
    payload["citations"] = [asdict(citation) for citation in record.citations]
    return payload


def _record_from_dict(payload: dict[str, Any]) -> MemoryRecord:
    citations = tuple(
        Citation(
            document_id=str(item["document_id"]),
            chunk_hash=str(item["chunk_hash"]),
            page=int(item["page"]),
            paragraph=int(item["paragraph"]),
            span_start=int(item["span_start"]),
            span_end=int(item["span_end"]),
            quote=str(item["quote"]),
            claim=str(item["claim"]),
            required_terms=tuple(str(term) for term in item.get("required_terms", [])),
            requires_time_claim=bool(item.get("requires_time_claim", False)),
        )
        for item in payload.get("citations", [])
    )
    clean = dict(payload)
    clean["citations"] = citations
    return MemoryRecord(**clean)
