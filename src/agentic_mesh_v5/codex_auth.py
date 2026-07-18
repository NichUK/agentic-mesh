from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Any, Callable

from openai_codex import Codex, CodexConfig


_MOUNT_REF = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_CONTAINER_HOME = PurePosixPath("/mesh/worker-auth/codex")
_CONFIGURATION_ERROR = "Codex OAuth cache binding is invalid"
_PROBE_ERROR = "Codex authentication status is unavailable"


class CodexOAuthConfigurationError(ValueError):
    pass


class CodexOAuthProbeError(RuntimeError):
    pass


class CodexOAuthState(str, Enum):
    AUTHENTICATED = "authenticated"
    SIGN_IN_REQUIRED = "sign_in_required"
    WRONG_METHOD = "wrong_method"


class CodexAccountKind(str, Enum):
    CHATGPT = "chatgpt"
    API_KEY = "api_key"


@dataclass(frozen=True, slots=True)
class CodexOAuthStatus:
    state: CodexOAuthState
    account_kind: CodexAccountKind | None


@dataclass(frozen=True, slots=True)
class CodexOAuthCacheBinding:
    mount_ref: str
    host_home: Path = field(repr=False)
    container_home: PurePosixPath = _CONTAINER_HOME

    def local_environment(self) -> Mapping[str, str]:
        return MappingProxyType({"CODEX_HOME": str(self.host_home)})

    def container_environment(self) -> Mapping[str, str]:
        return MappingProxyType({"CODEX_HOME": str(self.container_home)})


class CodexOAuthCacheResolver:
    def __init__(
        self,
        mounts: Mapping[str, Path],
        *,
        forbidden_roots: Iterable[Path],
    ) -> None:
        if not isinstance(mounts, Mapping):
            raise _configuration_error()
        roots = tuple(_absolute_root(root) for root in forbidden_roots)
        resolved: dict[str, Path] = {}
        for mount_ref, host_home in mounts.items():
            if not isinstance(mount_ref, str) or not _MOUNT_REF.fullmatch(mount_ref):
                raise _configuration_error()
            home = _existing_directory(host_home)
            if any(home == root or home.is_relative_to(root) for root in roots):
                raise _configuration_error()
            resolved[mount_ref] = home
        self._mounts = MappingProxyType(resolved)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(mount_count={len(self._mounts)})"

    def resolve(self, mount_ref: str) -> CodexOAuthCacheBinding:
        if not isinstance(mount_ref, str) or not _MOUNT_REF.fullmatch(mount_ref):
            raise _configuration_error()
        try:
            host_home = self._mounts[mount_ref]
        except KeyError:
            raise _configuration_error() from None
        return CodexOAuthCacheBinding(mount_ref, host_home)


class CodexOAuthProbe:
    def __init__(
        self,
        *,
        sdk_factory: Callable[[CodexConfig], Any] = Codex,
    ) -> None:
        self._sdk_factory = sdk_factory

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def check(self, binding: CodexOAuthCacheBinding) -> CodexOAuthStatus:
        if not isinstance(binding, CodexOAuthCacheBinding):
            raise _configuration_error()
        sdk: Any | None = None
        probe_failed = False
        try:
            sdk = self._sdk_factory(
                CodexConfig(
                    env=dict(binding.local_environment()),
                    client_name="agentic_mesh_v5",
                    client_title="Agentic Mesh V5",
                    client_version="0.1.0",
                    experimental_api=False,
                )
            )
            response = sdk.account(refresh_token=True)
            status = _status(response)
        except CodexOAuthConfigurationError:
            raise
        except Exception:
            if sdk is not None:
                _quiet_close(sdk)
            probe_failed = True
        if probe_failed:
            raise CodexOAuthProbeError(_PROBE_ERROR)
        close_failed = False
        try:
            sdk.close()
        except Exception:
            _quiet_close(sdk)
            close_failed = True
        if close_failed:
            raise CodexOAuthProbeError(_PROBE_ERROR)
        return status


def _status(response: object) -> CodexOAuthStatus:
    if not hasattr(response, "account"):
        raise CodexOAuthProbeError(_PROBE_ERROR)
    account = getattr(response, "account", None)
    if account is None:
        return CodexOAuthStatus(CodexOAuthState.SIGN_IN_REQUIRED, None)
    root = getattr(account, "root", account)
    account_type = getattr(root, "type", None)
    if account_type == "chatgpt":
        return CodexOAuthStatus(
            CodexOAuthState.AUTHENTICATED,
            CodexAccountKind.CHATGPT,
        )
    if account_type == "apiKey":
        return CodexOAuthStatus(
            CodexOAuthState.WRONG_METHOD,
            CodexAccountKind.API_KEY,
        )
    raise CodexOAuthProbeError(_PROBE_ERROR)


def _absolute_root(value: object) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise _configuration_error()
    return value.resolve()


def _existing_directory(value: object) -> Path:
    if not isinstance(value, Path) or not value.is_absolute() or not value.is_dir():
        raise _configuration_error()
    return value.resolve()


def _quiet_close(sdk: Any) -> None:
    try:
        sdk.close()
    except Exception:
        pass


def _configuration_error() -> CodexOAuthConfigurationError:
    return CodexOAuthConfigurationError(_CONFIGURATION_ERROR)
