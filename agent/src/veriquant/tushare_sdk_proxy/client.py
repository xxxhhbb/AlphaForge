"""Credential-safe SDK construction for one reviewed proxy endpoint."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit


ALLOWED_ENDPOINT = "https://ts.gyzcloud.top/api"


class TushareProxyError(RuntimeError):
    """Typed proxy configuration or execution failure."""


@dataclass(frozen=True)
class ProxySdkConfig:
    endpoint: str
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if (
            self.endpoint != ALLOWED_ENDPOINT
            or parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise TushareProxyError("Tushare proxy endpoint is not allowlisted")
        if not self.token.strip() or self.token == "your-tushare-token":
            raise TushareProxyError("Tushare credential is not configured")


def create_proxy_client(config: ProxySdkConfig, *, tushare_module: Any | None = None) -> Any:
    """Create a Tushare SDK client without exposing credential material."""

    if not isinstance(config, ProxySdkConfig):
        raise TushareProxyError("typed proxy SDK configuration is required")
    if tushare_module is None:
        try:
            import tushare as tushare_module
        except Exception as exc:
            raise TushareProxyError(
                f"Tushare SDK is unavailable ({type(exc).__name__})"
            ) from exc
    try:
        client = tushare_module.pro_api(config.token)
        setattr(client, "_DataApi__http_url", config.endpoint)
    except Exception as exc:
        raise TushareProxyError(
            f"Tushare SDK client initialization failed ({type(exc).__name__})"
        ) from exc
    return client
