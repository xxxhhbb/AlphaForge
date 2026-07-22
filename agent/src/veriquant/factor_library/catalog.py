"""Pure-static factor library catalog for VeriQuant.

The legacy Vibe factor zoo contains Python modules with ``compute`` functions.
This catalog deliberately treats those files as inert source artifacts:

* no importlib;
* no dynamic import;
* no eval/exec;
* no compute/callable access;
* no generated ``factor.py`` path.

Only literal ``__alpha_meta__`` assignments and file bytes are inspected.  A
later wave may port selected records into restricted FactorDAG templates, but
catalog discovery itself never grants official execution or evidence authority.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


CATALOG_SCHEMA_VERSION = "veriquant.factor-library-catalog/v1"
MAX_SOURCE_BYTES = 250_000

_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FACTOR_ID_RE = re.compile(r"^[a-z][a-z0-9]+_[a-z0-9_]+$")
_SAFE_TEXT_RE = re.compile(r"^[A-Za-z0-9_:/+.,#() -]{1,256}$")


class FactorQualificationStatus(str, Enum):
    """Static qualification states for library seeds."""

    DISCOVERED = "DISCOVERED"
    METADATA_VALIDATED = "METADATA_VALIDATED"
    DAG_PORTED = "DAG_PORTED"
    PIT_TESTED = "PIT_TESTED"
    AUXILIARY_ONLY = "AUXILIARY_ONLY"
    REJECTED = "REJECTED"


class CatalogFailureCode(str, Enum):
    """Typed fail-closed catalog build failures."""

    ROOT_UNAVAILABLE = "ROOT_UNAVAILABLE"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    INVALID_PATH = "INVALID_PATH"
    INVALID_IDENTIFIER = "INVALID_IDENTIFIER"
    SOURCE_TOO_LARGE = "SOURCE_TOO_LARGE"
    SOURCE_UNREADABLE = "SOURCE_UNREADABLE"
    MALFORMED_SOURCE = "MALFORMED_SOURCE"
    METADATA_MISSING = "METADATA_MISSING"
    METADATA_NOT_LITERAL = "METADATA_NOT_LITERAL"
    METADATA_INVALID = "METADATA_INVALID"
    DUPLICATE_FACTOR_ID = "DUPLICATE_FACTOR_ID"


class CatalogBuildError(Exception):
    """Raised when the catalog cannot be trusted."""

    def __init__(self, code: CatalogFailureCode, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _bytes_hash(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _fail(code: CatalogFailureCode, message: str, **details: Any) -> None:
    raise CatalogBuildError(code, message, details=details)


def _require_token(value: str, field: str) -> None:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        _fail(CatalogFailureCode.INVALID_IDENTIFIER, f"{field} is not a safe identifier", **{field: value})


def _require_factor_id(value: str) -> None:
    if not isinstance(value, str) or not _FACTOR_ID_RE.fullmatch(value):
        _fail(CatalogFailureCode.INVALID_IDENTIFIER, "factor id is not safe", factor_id=value)


def _safe_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_TEXT_RE.fullmatch(value):
        _fail(CatalogFailureCode.METADATA_INVALID, f"{field} contains unsupported text", value=value)
    if ".." in value or "\\" in value:
        _fail(CatalogFailureCode.METADATA_INVALID, f"{field} contains traversal-like text", value=value)
    return value


def _safe_string_tuple(raw: Any, field: str, *, allow_fund_fields: bool = False) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw or any(not isinstance(item, str) for item in raw):
        _fail(CatalogFailureCode.METADATA_INVALID, f"{field} must be a non-empty string list")
    out: list[str] = []
    for item in raw:
        if allow_fund_fields and item.startswith("fund:"):
            safe = _safe_text(item, field)
        else:
            safe = _safe_text(item, field)
        out.append(safe.lower() if field in {"themes", "markets", "frequency"} else safe)
    return tuple(sorted(dict.fromkeys(out)))


def _extract_literal_alpha_meta(path: Path, source_text: str) -> Mapping[str, Any]:
    try:
        tree = ast.parse(source_text, filename=str(path))
    except SyntaxError as exc:
        _fail(
            CatalogFailureCode.MALFORMED_SOURCE,
            "source cannot be parsed",
            path=str(path),
            error=f"{exc.__class__.__name__}: {exc.msg}",
        )
    meta_node: ast.expr | None = None
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "__alpha_meta__" for target in stmt.targets):
            meta_node = stmt.value
            break
    if meta_node is None:
        _fail(CatalogFailureCode.METADATA_MISSING, "__alpha_meta__ assignment not found", path=str(path))
    try:
        raw = ast.literal_eval(meta_node)
    except (SyntaxError, ValueError) as exc:
        _fail(
            CatalogFailureCode.METADATA_NOT_LITERAL,
            "__alpha_meta__ must be a literal mapping",
            path=str(path),
            error=type(exc).__name__,
        )
    if not isinstance(raw, Mapping):
        _fail(CatalogFailureCode.METADATA_INVALID, "__alpha_meta__ must be a mapping", path=str(path))
    return raw


def _infer_family(zoo: str, factor_id: str, themes: Sequence[str]) -> str:
    if zoo == "qlib158":
        return re.sub(r"\d+$", "", factor_id).rstrip("_") or factor_id
    if zoo in {"alpha101", "gtja191"}:
        return f"{zoo}_formulaic_alpha"
    if zoo == "fundamental":
        return "fundamental_" + (themes[0] if themes else "general")
    if zoo == "academic":
        return "academic_" + (themes[0] if themes else "general")
    return f"{zoo}_general"


def _metadata_payload(meta: Mapping[str, Any]) -> dict[str, Any]:
    factor_id = meta.get("id")
    _require_factor_id(factor_id)
    themes = _safe_string_tuple(meta.get("theme"), "themes")
    fields = _safe_string_tuple(meta.get("columns_required"), "fields", allow_fund_fields=True)
    markets = _safe_string_tuple(meta.get("universe"), "markets")
    frequency = _safe_string_tuple(meta.get("frequency"), "frequency")
    warmup = meta.get("min_warmup_bars")
    if isinstance(warmup, bool) or not isinstance(warmup, int) or warmup < 0 or warmup > 10_000:
        _fail(CatalogFailureCode.METADATA_INVALID, "min_warmup_bars must be a bounded non-negative integer", factor_id=factor_id)
    decay_horizon = meta.get("decay_horizon", 0)
    if isinstance(decay_horizon, bool) or not isinstance(decay_horizon, int) or decay_horizon < 0 or decay_horizon > 10_000:
        _fail(CatalogFailureCode.METADATA_INVALID, "decay_horizon must be a bounded non-negative integer", factor_id=factor_id)
    return {
        "factor_id": factor_id,
        "themes": themes,
        "fields": fields,
        "markets": markets,
        "frequency": frequency,
        "warmup": warmup,
        "decay_horizon": decay_horizon,
        "nickname": meta.get("nickname") if isinstance(meta.get("nickname"), str) else None,
        "requires_sector": bool(meta.get("requires_sector", False)),
        "extras_required": _safe_string_tuple(meta.get("extras_required", []), "fields", allow_fund_fields=True)
        if meta.get("extras_required", []) else (),
    }


@dataclass(frozen=True, kw_only=True)
class FactorLibraryRecord:
    """One static library seed discovered from source metadata."""

    factor_id: str
    zoo: str
    themes: tuple[str, ...]
    markets: tuple[str, ...]
    fields: tuple[str, ...]
    frequency: tuple[str, ...]
    warmup: int
    source_relpath: str
    source_hash: str
    metadata_hash: str
    family: str
    qualification_status: FactorQualificationStatus
    nickname: str | None = None
    decay_horizon: int = 0
    requires_sector: bool = False
    extras_required: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_factor_id(self.factor_id)
        _require_token(self.zoo, "zoo")
        if self.qualification_status not in set(FactorQualificationStatus):
            _fail(CatalogFailureCode.METADATA_INVALID, "invalid qualification status", factor_id=self.factor_id)
        if "\\" in self.source_relpath or ".." in Path(self.source_relpath).parts:
            _fail(CatalogFailureCode.PATH_TRAVERSAL, "unsafe source relative path", path=self.source_relpath)

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "zoo": self.zoo,
            "themes": list(self.themes),
            "markets": list(self.markets),
            "fields": list(self.fields),
            "frequency": list(self.frequency),
            "warmup": self.warmup,
            "source_relpath": self.source_relpath,
            "source_hash": self.source_hash,
            "metadata_hash": self.metadata_hash,
            "family": self.family,
            "qualification_status": self.qualification_status.value,
            "nickname": self.nickname,
            "decay_horizon": self.decay_horizon,
            "requires_sector": self.requires_sector,
            "extras_required": list(self.extras_required),
        }


@dataclass(frozen=True, kw_only=True)
class FactorLibraryCatalog:
    """Stable, static catalog of factor-library metadata."""

    records: tuple[FactorLibraryRecord, ...]
    schema_version: str = CATALOG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CATALOG_SCHEMA_VERSION:
            _fail(CatalogFailureCode.METADATA_INVALID, "unsupported catalog schema version")
        seen: set[str] = set()
        ordered = tuple(sorted(self.records, key=lambda record: record.factor_id))
        for record in ordered:
            if record.factor_id in seen:
                _fail(CatalogFailureCode.DUPLICATE_FACTOR_ID, "duplicate factor id", factor_id=record.factor_id)
            seen.add(record.factor_id)
        object.__setattr__(self, "records", ordered)

    @property
    def total_records(self) -> int:
        return len(self.records)

    @property
    def catalog_hash(self) -> str:
        return _content_hash(self.to_dict())

    @property
    def counts_by_zoo(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.zoo] = counts.get(record.zoo, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def counts_by_status(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            status = record.qualification_status.value
            counts[status] = counts.get(status, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        """Return the stable hash payload.

        Intentionally excludes ``generated_at`` and other wall-clock fields.
        """

        return {
            "schema_version": self.schema_version,
            "records": [record.to_dict() for record in self.records],
            "counts": {
                "total": self.total_records,
                "by_zoo": self.counts_by_zoo,
                "by_status": self.counts_by_status,
            },
        }

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "catalog_hash": self.catalog_hash,
            "total_records": self.total_records,
            "counts_by_zoo": self.counts_by_zoo,
            "counts_by_status": self.counts_by_status,
        }


class FactorLibraryCatalogBuilder:
    """Build a pure-static catalog from a factor-zoo source tree."""

    def __init__(self, zoo_root: Path | str | None = None) -> None:
        if zoo_root is None:
            zoo_root = Path(__file__).resolve().parents[2] / "factors" / "zoo"
        self.zoo_root = Path(zoo_root)

    def build(self, *, paths: Iterable[Path | str] | None = None) -> FactorLibraryCatalog:
        root = self._root()
        source_paths = tuple(self._iter_source_paths(root) if paths is None else (Path(path) for path in paths))
        records = [self._record_from_path(root, path) for path in source_paths]
        return FactorLibraryCatalog(records=tuple(records))

    def _root(self) -> Path:
        try:
            root = self.zoo_root.resolve(strict=True)
        except OSError as exc:
            _fail(CatalogFailureCode.ROOT_UNAVAILABLE, "factor zoo root is unavailable", path=str(self.zoo_root), error=type(exc).__name__)
        if not root.is_dir():
            _fail(CatalogFailureCode.ROOT_UNAVAILABLE, "factor zoo root is not a directory", path=str(root))
        return root

    @staticmethod
    def _iter_source_paths(root: Path) -> Iterable[Path]:
        for zoo_dir in sorted(root.iterdir(), key=lambda path: path.name):
            if not zoo_dir.is_dir() or zoo_dir.name.startswith("_"):
                continue
            for py_path in sorted(zoo_dir.glob("*.py"), key=lambda path: path.name):
                if py_path.name == "__init__.py" or py_path.name.startswith("_"):
                    continue
                yield py_path

    @staticmethod
    def _safe_relative(root: Path, path: Path) -> tuple[Path, str]:
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            _fail(CatalogFailureCode.INVALID_PATH, "source path is unavailable", path=str(path), error=type(exc).__name__)
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            _fail(CatalogFailureCode.PATH_TRAVERSAL, "source path escapes factor zoo root", path=str(path))
        if resolved.suffix != ".py":
            _fail(CatalogFailureCode.INVALID_PATH, "source path must be a Python metadata file", path=str(path))
        rel_posix = rel.as_posix()
        if rel_posix.endswith("/__init__.py") or rel_posix.startswith("_") or "/_" in rel_posix:
            _fail(CatalogFailureCode.INVALID_PATH, "source path is not a factor module", path=rel_posix)
        return resolved, rel_posix

    def _record_from_path(self, root: Path, path: Path) -> FactorLibraryRecord:
        resolved, rel_posix = self._safe_relative(root, path)
        parts = Path(rel_posix).parts
        if len(parts) != 2:
            _fail(CatalogFailureCode.INVALID_PATH, "factor source must live directly below a zoo directory", path=rel_posix)
        zoo, file_name = parts
        _require_token(zoo, "zoo")
        _require_token(Path(file_name).stem, "factor_file")
        try:
            source_bytes = resolved.read_bytes()
        except OSError as exc:
            _fail(CatalogFailureCode.SOURCE_UNREADABLE, "cannot read factor source", path=rel_posix, error=type(exc).__name__)
        if len(source_bytes) > MAX_SOURCE_BYTES:
            _fail(CatalogFailureCode.SOURCE_TOO_LARGE, "factor source exceeds size cap", path=rel_posix, bytes=len(source_bytes))
        try:
            source_text = source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            _fail(CatalogFailureCode.SOURCE_UNREADABLE, "factor source must be UTF-8", path=rel_posix, error=type(exc).__name__)
        meta = _extract_literal_alpha_meta(resolved, source_text)
        payload = _metadata_payload(meta)
        factor_id = str(payload["factor_id"])
        themes = tuple(payload["themes"])
        fields = tuple(payload["fields"])
        markets = tuple(payload["markets"])
        frequency = tuple(payload["frequency"])
        family = _infer_family(zoo, factor_id, themes)
        metadata_hash = _content_hash(
            {
                "schema_version": CATALOG_SCHEMA_VERSION,
                "zoo": zoo,
                "source_relpath": rel_posix,
                "metadata": payload,
                "family": family,
            }
        )
        return FactorLibraryRecord(
            factor_id=factor_id,
            zoo=zoo,
            themes=themes,
            markets=markets,
            fields=fields,
            frequency=frequency,
            warmup=int(payload["warmup"]),
            source_relpath=rel_posix,
            source_hash=_bytes_hash(source_bytes),
            metadata_hash=metadata_hash,
            family=family,
            qualification_status=FactorQualificationStatus.METADATA_VALIDATED,
            nickname=payload["nickname"],
            decay_horizon=int(payload["decay_horizon"]),
            requires_sector=bool(payload["requires_sector"]),
            extras_required=tuple(payload["extras_required"]),
        )


def build_factor_library_catalog(zoo_root: Path | str | None = None) -> FactorLibraryCatalog:
    """Build the default static catalog.

    The result is deterministic for identical source files and paths.  Counts
    are derived from the build result itself, so bundled governance labels such
    as "Qlib158" do not override the actual discovered count.
    """

    return FactorLibraryCatalogBuilder(zoo_root).build()


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "CatalogBuildError",
    "CatalogFailureCode",
    "FactorLibraryCatalog",
    "FactorLibraryCatalogBuilder",
    "FactorLibraryRecord",
    "FactorQualificationStatus",
    "build_factor_library_catalog",
]
