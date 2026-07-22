"""Canonical serialization and fail-closed validation primitives."""

from __future__ import annotations

import collections.abc
import hashlib
import json
import math
import re
import types
from dataclasses import MISSING, fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, TypeVar, Union, get_args, get_origin, get_type_hints

from .authority import validate_component_payload
from .enums import Component, ContractValidationError, FailureCode


_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
_T = TypeVar("_T", bound="CanonicalContract")


def fail(code: FailureCode, message: str, **details: Any) -> None:
    raise ContractValidationError(code, message, details=details)


def require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a non-empty string", field=field_name)


def require_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        fail(
            FailureCode.INVALID_IDENTIFIER,
            f"{field_name} is not a stable identifier",
            field=field_name,
            value=value,
        )


def require_hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        fail(
            FailureCode.INVALID_HASH,
            f"{field_name} must use sha256:<64 lowercase hex>",
            field=field_name,
        )


def require_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        fail(
            FailureCode.INVALID_TIMESTAMP,
            f"{field_name} must be a timezone-aware datetime",
            field=field_name,
        )


def require_time_order(*ordered: tuple[str, datetime]) -> None:
    for name, value in ordered:
        require_aware_datetime(value, name)
    for (left_name, left), (right_name, right) in zip(ordered, ordered[1:]):
        if left > right:
            fail(
                FailureCode.INVALID_TIME_ORDER,
                f"{left_name} must not be after {right_name}",
                left=left_name,
                right=right_name,
            )


def require_nonempty_tuple(value: tuple[Any, ...], field_name: str) -> None:
    if not isinstance(value, tuple) or not value:
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a non-empty tuple", field=field_name)


def require_finite_number(value: int | float | Decimal, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be numeric", field=field_name)
    if isinstance(value, float) and not math.isfinite(value):
        fail(FailureCode.NON_FINITE_NUMBER, f"{field_name} must be finite", field=field_name)
    if isinstance(value, Decimal) and not value.is_finite():
        fail(FailureCode.NON_FINITE_NUMBER, f"{field_name} must be finite", field=field_name)


def freeze_mapping(
    value: Mapping[str, Any], field_name: str, *, numeric_values: bool = False
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        fail(FailureCode.INVALID_VALUE, f"{field_name} must be a mapping", field=field_name)
    copied: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            fail(FailureCode.INVALID_VALUE, f"{field_name} keys must be non-empty strings", field=field_name)
        if numeric_values:
            require_finite_number(item, f"{field_name}.{key}")
        copied[key] = item
    return MappingProxyType(copied)


def _canonical_datetime(value: datetime) -> str:
    require_aware_datetime(value, "datetime")
    utc = value.astimezone(timezone.utc)
    rendered = utc.isoformat(timespec="microseconds")
    return rendered[:-6] + "Z"


def canonicalize(value: Any) -> Any:
    """Convert supported values into an unambiguous JSON-compatible tree."""

    if isinstance(value, CanonicalContract):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _canonical_datetime(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            fail(FailureCode.NON_FINITE_NUMBER, "Decimal value must be finite")
        return format(value, "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            fail(FailureCode.NON_FINITE_NUMBER, "float value must be finite")
        return 0.0 if value == 0.0 else value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                fail(FailureCode.INVALID_VALUE, "canonical mapping keys must be strings")
            normalized[key] = canonicalize(item)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (tuple, list)):
        return [canonicalize(item) for item in value]
    if is_dataclass(value):
        return {field.name: canonicalize(getattr(value, field.name)) for field in fields(value)}
    fail(FailureCode.INVALID_VALUE, f"unsupported canonical value type: {type(value).__name__}")


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        require_aware_datetime(value, field_name)
        return value
    if not isinstance(value, str):
        fail(FailureCode.INVALID_TIMESTAMP, f"{field_name} must be an ISO-8601 timestamp")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ContractValidationError(
            FailureCode.INVALID_TIMESTAMP,
            f"{field_name} must be an ISO-8601 timestamp",
            details={"field": field_name},
        ) from exc
    require_aware_datetime(parsed, field_name)
    return parsed


def _coerce_value(value: Any, annotation: Any, field_name: str) -> Any:
    if annotation is Any:
        return value
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        errors: list[Exception] = []
        for option in args:
            if option is type(None):
                continue
            try:
                return _coerce_value(value, option, field_name)
            except (ContractValidationError, TypeError, ValueError) as exc:
                errors.append(exc)
        fail(FailureCode.INVALID_VALUE, f"{field_name} does not match its declared union")
    if annotation is datetime:
        return _parse_datetime(value, field_name)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        try:
            return annotation(value)
        except (TypeError, ValueError) as exc:
            raise ContractValidationError(
                FailureCode.INVALID_VALUE,
                f"{field_name} has an unknown enum value",
                details={"field": field_name, "value": value},
            ) from exc
    if origin is tuple:
        if not isinstance(value, (tuple, list)):
            fail(FailureCode.INVALID_VALUE, f"{field_name} must be an array")
        item_type = args[0] if args else Any
        if len(args) > 1 and args[1] is not Ellipsis and len(args) != len(value):
            fail(FailureCode.INVALID_VALUE, f"{field_name} has the wrong tuple length")
        if len(args) > 1 and args[1] is not Ellipsis:
            return tuple(
                _coerce_value(item, option, f"{field_name}[{index}]")
                for index, (item, option) in enumerate(zip(value, args))
            )
        return tuple(
            _coerce_value(item, item_type, f"{field_name}[{index}]")
            for index, item in enumerate(value)
        )
    if origin in (dict, Mapping, collections.abc.Mapping):
        if not isinstance(value, Mapping):
            fail(FailureCode.INVALID_VALUE, f"{field_name} must be an object")
        key_type, value_type = args if len(args) == 2 else (str, Any)
        return {
            _coerce_value(key, key_type, f"{field_name}.key"): _coerce_value(
                item, value_type, f"{field_name}.{key}"
            )
            for key, item in value.items()
        }
    if isinstance(annotation, type) and issubclass(annotation, CanonicalContract):
        if not isinstance(value, Mapping):
            fail(FailureCode.INVALID_VALUE, f"{field_name} must be a contract object")
        return annotation.from_payload(value)
    if isinstance(annotation, type) and is_dataclass(annotation):
        if not isinstance(value, Mapping):
            fail(FailureCode.INVALID_VALUE, f"{field_name} must be an object")
        hints = get_type_hints(annotation)
        allowed = {field.name for field in fields(annotation)}
        unknown = set(value) - allowed
        if unknown:
            fail(
                FailureCode.UNKNOWN_FIELD,
                f"{field_name} contains unknown fields",
                fields=tuple(sorted(unknown)),
            )
        kwargs = {
            name: _coerce_value(item, hints.get(name, Any), f"{field_name}.{name}")
            for name, item in value.items()
        }
        return annotation(**kwargs)
    if annotation in (str, int, float, bool):
        if type(value) is not annotation:
            fail(FailureCode.INVALID_VALUE, f"{field_name} must be {annotation.__name__}")
        return value
    return value


class CanonicalContract:
    """Mixin for immutable, versioned, canonical contract dataclasses."""

    SCHEMA_VERSION: ClassVar[str] = "1.0.0"
    ALLOWED_PRODUCERS: ClassVar[frozenset[Component] | None] = None

    @property
    def schema_version(self) -> str:
        return self.SCHEMA_VERSION

    @property
    def contract_type(self) -> str:
        return type(self).__name__

    def to_dict(self) -> dict[str, Any]:
        if not is_dataclass(self):
            fail(FailureCode.INVALID_VALUE, "CanonicalContract subclasses must be dataclasses")
        data = {
            field.name: canonicalize(getattr(self, field.name))
            for field in fields(self)
        }
        return {
            "contract_type": self.contract_type,
            "schema_version": self.schema_version,
            **data,
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    def canonical_bytes(self) -> bytes:
        return self.canonical_json().encode("utf-8")

    @property
    def artifact_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_payload(
        cls: type[_T],
        payload: Mapping[str, Any],
        *,
        component: Component | str | None = None,
    ) -> _T:
        if not isinstance(payload, Mapping):
            fail(FailureCode.INVALID_VALUE, "contract payload must be an object")
        raw = dict(payload)
        if "schema_version" not in raw:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "schema_version is required", field="schema_version")
        if raw["schema_version"] != cls.SCHEMA_VERSION:
            fail(
                FailureCode.INVALID_SCHEMA_VERSION,
                f"unsupported schema version for {cls.__name__}",
                expected=cls.SCHEMA_VERSION,
                actual=raw["schema_version"],
            )
        if "contract_type" not in raw:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "contract_type is required", field="contract_type")
        if raw["contract_type"] != cls.__name__:
            fail(
                FailureCode.INVALID_VALUE,
                f"contract_type must be {cls.__name__}",
                actual=raw["contract_type"],
            )

        origin = Component(component) if component is not None else None
        if origin is None:
            for candidate in ("produced_by", "source_component", "approver_component", "component"):
                if candidate in raw:
                    try:
                        origin = Component(raw[candidate])
                    except (TypeError, ValueError):
                        pass
                    break
        if origin is not None:
            # Run authority checks before unknown-field checks.  Unauthorized fields
            # must not be smuggled in as merely "extra" descriptive metadata.
            validate_component_payload(origin, raw)
            allowed = cls.ALLOWED_PRODUCERS
            if allowed is not None and origin not in allowed:
                fail(
                    FailureCode.INVALID_ARTIFACT_PRODUCER,
                    f"{origin.value} cannot produce {cls.__name__}",
                    component=origin.value,
                    contract_type=cls.__name__,
                )

        raw.pop("schema_version")
        raw.pop("contract_type")
        model_fields = {field.name: field for field in fields(cls)}
        unknown = set(raw) - set(model_fields)
        if unknown:
            fail(
                FailureCode.UNKNOWN_FIELD,
                f"{cls.__name__} contains unknown fields",
                fields=tuple(sorted(unknown)),
            )
        for name, field in model_fields.items():
            if name not in raw and field.default is MISSING and field.default_factory is MISSING:
                fail(
                    FailureCode.MISSING_REQUIRED_FIELD,
                    f"{name} is required for {cls.__name__}",
                    field=name,
                )
        hints = get_type_hints(cls)
        kwargs = {
            name: _coerce_value(value, hints.get(name, Any), name)
            for name, value in raw.items()
        }
        return cls(**kwargs)

