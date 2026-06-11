from __future__ import annotations

import os
from urllib.parse import urlparse


def configured_url_root() -> str | None:
    value = os.environ.get("AGENTIC_MESH_URL_ROOT")
    if value:
        return value.strip().rstrip("/")
    return None


def configured_status_base_url() -> str | None:
    value = os.environ.get("AGENTIC_MESH_STATUS_BASE_URL") or configured_url_root()
    if value:
        return value.strip().rstrip("/")
    return None


def configured_auth_admin_url(
    default: str = "http://127.0.0.1:8100/auth/credentials",
) -> str:
    value = os.environ.get("AGENTIC_MESH_AUTH_ADMIN_URL")
    if value:
        return value.strip()
    root = configured_url_root()
    if root:
        return f"{root}/auth/credentials"
    return default


def approved_status_base_url(value: str | None = None) -> str:
    root = value or configured_status_base_url()
    if not root:
        return ""
    parsed = urlparse(str(root).strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or _is_loopback_host(parsed.hostname)
    ):
        return ""
    if parsed.path and parsed.path != "/":
        return ""
    return str(root).strip().rstrip("/")


def _is_loopback_host(hostname: str | None) -> bool:
    host = (hostname or "").lower()
    return host in {"localhost", "::1"} or host.startswith("127.")
