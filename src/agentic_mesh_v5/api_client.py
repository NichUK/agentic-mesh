from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit
import uuid

import httpx


API_TOKEN_FILE_ENV = "AGENTIC_MESH_V5_API_TOKEN_FILE"
API_URL_ENV = "AGENTIC_MESH_V5_API_URL"
SENSITIVE_KEY_PATTERN = re.compile(
    r"(^|[_-])(authorization|credential|password|secret|token)([_-]|$)", re.IGNORECASE
)


class ApiClientConfigurationError(RuntimeError):
    pass


class ApiCallError(RuntimeError):
    def __init__(
        self,
        *,
        exit_code: int,
        code: str,
        detail: str,
        action_id: str,
        status_code: int | None = None,
        result: object | None = None,
    ) -> None:
        super().__init__(detail)
        self.exit_code = exit_code
        self.code = code
        self.detail = detail
        self.action_id = action_id
        self.status_code = status_code
        self.result = result

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "runtime": "agentic-mesh-v5",
            "status": "rejected",
            "action_id": self.action_id,
            "error": {"code": self.code, "detail": self.detail},
        }
        if self.status_code is not None:
            payload["http_status"] = self.status_code
        if self.result is not None:
            payload["result"] = self.result
        return payload


@dataclass(frozen=True)
class ControlResult:
    method: str
    path: str
    status_code: int
    action_id: str
    result: object

    def to_dict(self) -> dict[str, object]:
        status = "planned" if _is_planned(self.result) else "ok"
        return {
            "runtime": "agentic-mesh-v5",
            "status": status,
            "action_id": self.action_id,
            "http_status": self.status_code,
            "method": self.method,
            "path": self.path,
            "result": self.result,
        }


class ControlApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        token_file: Path,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = _validated_base_url(base_url)
        self._token = _load_token(token_file)
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ApiClientConfigurationError("timeout_seconds must be positive")
        self._timeout = float(timeout_seconds)
        self._transport = transport

    @classmethod
    def from_environment(cls) -> ControlApiClient:
        base_url = os.environ.get(API_URL_ENV, "").strip()
        token_path = os.environ.get(API_TOKEN_FILE_ENV, "").strip()
        if not base_url:
            raise ApiClientConfigurationError(f"{API_URL_ENV} is required")
        if not token_path:
            raise ApiClientConfigurationError(f"{API_TOKEN_FILE_ENV} is required")
        return cls(base_url=base_url, token_file=Path(token_path))

    def call(
        self,
        *,
        method: str,
        path: str,
        body_file: Path | None = None,
    ) -> ControlResult:
        normalized_method = method.upper()
        if normalized_method not in {"GET", "POST"}:
            raise ApiClientConfigurationError("control method must be GET or POST")
        normalized_path = _validated_api_path(path)
        if normalized_method == "GET" and body_file is not None:
            raise ApiClientConfigurationError("GET control calls cannot include a body")
        body = None if body_file is None else _load_body(body_file)
        action_id = uuid.uuid4().hex
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json, application/problem+json",
            "X-Request-ID": action_id,
        }
        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = client.request(
                    normalized_method,
                    normalized_path,
                    headers=headers,
                    **({"json": body} if body is not None else {}),
                )
        except httpx.RequestError as exc:
            raise ApiCallError(
                exit_code=5,
                code="service_unavailable",
                detail="control API request failed",
                action_id=action_id,
            ) from exc
        if response.headers.get("x-request-id") != action_id:
            raise ApiCallError(
                exit_code=5,
                code="invalid_api_response",
                detail="control API did not confirm the action identifier",
                action_id=action_id,
                status_code=response.status_code,
            )
        parsed = _response_json(response, action_id)
        safe = redact_payload(parsed, secret_value=self._token)
        if 200 <= response.status_code < 300:
            return ControlResult(
                method=normalized_method,
                path=normalized_path,
                status_code=response.status_code,
                action_id=action_id,
                result=safe,
            )
        detail = (
            safe.get("detail", "control API rejected the request")
            if isinstance(safe, Mapping)
            else "control API rejected the request"
        )
        code = _problem_code(safe)
        raise ApiCallError(
            exit_code=_exit_code(response.status_code),
            code=code,
            detail=str(detail),
            action_id=action_id,
            status_code=response.status_code,
            result=safe,
        )


def redact_payload(value: object, *, secret_value: str = "") -> object:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "<redacted>"
                if SENSITIVE_KEY_PATTERN.search(str(key))
                else redact_payload(item, secret_value=secret_value)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(item, secret_value=secret_value) for item in value]
    if isinstance(value, str) and secret_value:
        return value.replace(secret_value, "<redacted>")
    return value


def _validated_base_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ApiClientConfigurationError("API URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ApiClientConfigurationError("API URL must not contain credentials")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ApiClientConfigurationError("API URL must contain only scheme and authority")
    loopback = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and not loopback:
        raise ApiClientConfigurationError("plaintext API transport is allowed only on loopback")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _validated_api_path(value: str) -> str:
    parsed = urlsplit(value.strip())
    path = parsed.path
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "\\" in path
        or not path.startswith("/api/v1/")
    ):
        raise ApiClientConfigurationError("control path must be below /api/v1 without a query")
    segments = [unquote(segment) for segment in path.split("/")]
    if any(segment in {".", ".."} or "/" in segment or "\\" in segment for segment in segments):
        raise ApiClientConfigurationError("control path must not contain traversal segments")
    return path


def _load_token(path: Path) -> str:
    if not path.exists() or not path.is_file():
        raise ApiClientConfigurationError("API token path must be an existing file")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ApiClientConfigurationError("API token file must be readable UTF-8") from exc
    if not token or len(token) > 8192 or any(
        ord(character) < 0x21 or ord(character) > 0x7E for character in token
    ):
        raise ApiClientConfigurationError("API token file must contain one opaque ASCII token")
    return token


def _load_body(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise ApiClientConfigurationError("control body path must be an existing file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ApiClientConfigurationError("control body file must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ApiClientConfigurationError("control body must be a JSON object")
    return payload


def _response_json(response: httpx.Response, action_id: str) -> object:
    try:
        return response.json()
    except ValueError as exc:
        raise ApiCallError(
            exit_code=5,
            code="invalid_api_response",
            detail="control API returned a non-JSON response",
            action_id=action_id,
            status_code=response.status_code,
        ) from exc


def _problem_code(payload: object) -> str:
    if isinstance(payload, Mapping):
        problem_type = payload.get("type")
        if isinstance(problem_type, str) and problem_type.startswith("urn:agentic-mesh:error:"):
            return problem_type.rsplit(":", 1)[-1]
    return "request_rejected"


def _exit_code(status_code: int) -> int:
    if status_code in {401, 403}:
        return 3
    if status_code in {404, 409, 422} or 400 <= status_code < 500:
        return 4
    return 5


def _is_planned(value: object) -> bool:
    return isinstance(value, Mapping) and value.get("status") == "planned"
