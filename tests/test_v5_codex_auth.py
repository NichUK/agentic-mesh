from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agentic_mesh_v5.codex_auth import CodexAccountKind
from agentic_mesh_v5.codex_auth import CodexOAuthCacheResolver
from agentic_mesh_v5.codex_auth import CodexOAuthConfigurationError
from agentic_mesh_v5.codex_auth import CodexOAuthProbe
from agentic_mesh_v5.codex_auth import CodexOAuthProbeError
from agentic_mesh_v5.codex_auth import CodexOAuthState


ROOT = Path(__file__).resolve().parents[1]


class FakeSdk:
    def __init__(self, account: object = None, *, error: Exception | None = None):
        self.response = SimpleNamespace(account=account)
        self.error = error
        self.refresh_requests: list[bool] = []
        self.close_count = 0

    def account(self, *, refresh_token: bool):
        self.refresh_requests.append(refresh_token)
        if self.error is not None:
            raise self.error
        return self.response

    def close(self) -> None:
        self.close_count += 1


def _account(account_type: str, **private_fields: object) -> object:
    return SimpleNamespace(
        root=SimpleNamespace(type=account_type, **private_fields)
    )


def test_resolves_external_named_cache_for_local_and_container_workers(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    cache = tmp_path / "private-secret-cache"
    cache.mkdir()
    mounts = {"current-codex": cache}
    resolver = CodexOAuthCacheResolver(mounts, forbidden_roots=[repository])
    mounts["current-codex"] = repository

    first = resolver.resolve("current-codex")
    second = resolver.resolve("current-codex")

    assert first is not second
    assert first == second
    assert first.local_environment() == {"CODEX_HOME": str(cache.resolve())}
    assert first.container_environment() == {
        "CODEX_HOME": "/mesh/worker-auth/codex"
    }
    with pytest.raises(TypeError):
        first.local_environment()["CODEX_HOME"] = "changed"  # type: ignore[index]
    assert "private-secret-cache" not in repr(first)
    assert "private-secret-cache" not in repr(resolver)


@pytest.mark.parametrize(
    "mounts,forbidden_roots",
    [
        ({"UPPER": Path("unused")}, []),
        ({"bad.name": Path("unused")}, []),
        ({"valid": "not-a-path"}, []),
        ({"valid": Path("relative")}, []),
    ],
)
def test_rejects_invalid_registry_entries(
    mounts: object,
    forbidden_roots: list[Path],
) -> None:
    with pytest.raises(
        CodexOAuthConfigurationError,
        match="Codex OAuth cache binding is invalid",
    ):
        CodexOAuthCacheResolver(  # type: ignore[arg-type]
            mounts,
            forbidden_roots=forbidden_roots,
        )


def test_rejects_missing_files_and_repository_contained_caches(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    cache = repository / "state" / "codex"
    cache.mkdir(parents=True)
    file_cache = tmp_path / "file-cache"
    file_cache.write_text("private credential", encoding="utf-8")

    for candidate in (cache, file_cache, tmp_path / "missing"):
        with pytest.raises(CodexOAuthConfigurationError) as captured:
            CodexOAuthCacheResolver(
                {"current": candidate},
                forbidden_roots=[repository],
            )
        assert str(candidate) not in str(captured.value)
        assert "private credential" not in str(captured.value)


def test_resolved_symlink_cannot_bypass_forbidden_root(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    cache = repository / "cache"
    cache.mkdir(parents=True)
    link = tmp_path / "external-link"
    try:
        link.symlink_to(cache, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(CodexOAuthConfigurationError):
        CodexOAuthCacheResolver(
            {"current": link},
            forbidden_roots=[repository],
        )


def test_unknown_or_invalid_mount_reference_fails_closed(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    resolver = CodexOAuthCacheResolver({"current": cache}, forbidden_roots=[])

    for mount_ref in ("missing", "../current", ""):
        with pytest.raises(CodexOAuthConfigurationError) as captured:
            resolver.resolve(mount_ref)
        assert str(cache) not in str(captured.value)


def test_two_workers_reuse_one_cache_and_refresh_without_exposing_identity(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "private-secret-cache"
    cache.mkdir()
    binding = CodexOAuthCacheResolver(
        {"shared-role": cache}, forbidden_roots=[]
    ).resolve("shared-role")
    sdks = [
        FakeSdk(_account("chatgpt", email="first-secret@example.test")),
        FakeSdk(_account("chatgpt", email="second-secret@example.test")),
    ]
    configs = []

    def factory(config):
        configs.append(config)
        return sdks[len(configs) - 1]

    probe = CodexOAuthProbe(sdk_factory=factory)
    statuses = [probe.check(binding), probe.check(binding)]

    assert all(status.state is CodexOAuthState.AUTHENTICATED for status in statuses)
    assert all(status.account_kind is CodexAccountKind.CHATGPT for status in statuses)
    assert [sdk.refresh_requests for sdk in sdks] == [[True], [True]]
    assert [sdk.close_count for sdk in sdks] == [1, 1]
    assert [config.env for config in configs] == [
        {"CODEX_HOME": str(cache.resolve())},
        {"CODEX_HOME": str(cache.resolve())},
    ]
    combined = repr((binding, probe, statuses))
    assert "private-secret-cache" not in combined
    assert "example.test" not in combined


@pytest.mark.parametrize(
    "account,state,kind",
    [
        (None, CodexOAuthState.SIGN_IN_REQUIRED, None),
        (
            _account("apiKey", api_key="private-secret"),
            CodexOAuthState.WRONG_METHOD,
            CodexAccountKind.API_KEY,
        ),
    ],
)
def test_expired_or_wrong_method_status_is_safe(
    tmp_path: Path,
    account: object,
    state: CodexOAuthState,
    kind: CodexAccountKind | None,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    binding = CodexOAuthCacheResolver(
        {"current": cache}, forbidden_roots=[]
    ).resolve("current")
    sdk = FakeSdk(account)

    status = CodexOAuthProbe(sdk_factory=lambda _config: sdk).check(binding)

    assert status.state is state
    assert status.account_kind is kind
    assert sdk.refresh_requests == [True]
    assert sdk.close_count == 1
    assert "private-secret" not in repr(status)


def test_probe_failures_are_redacted_and_cleanup_is_deterministic(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    binding = CodexOAuthCacheResolver(
        {"current": cache}, forbidden_roots=[]
    ).resolve("current")

    with pytest.raises(CodexOAuthProbeError) as startup:
        CodexOAuthProbe(
            sdk_factory=lambda _config: (_ for _ in ()).throw(
                RuntimeError("private startup token")
            )
        ).check(binding)
    assert str(startup.value) == "Codex authentication status is unavailable"
    assert startup.value.__cause__ is None
    assert startup.value.__context__ is None

    account_failure = FakeSdk(error=RuntimeError("private expired token"))
    with pytest.raises(CodexOAuthProbeError) as account:
        CodexOAuthProbe(sdk_factory=lambda _config: account_failure).check(binding)
    assert "private" not in str(account.value)
    assert account.value.__context__ is None
    assert account_failure.close_count == 1

    class CloseFailureSdk(FakeSdk):
        def close(self) -> None:
            self.close_count += 1
            raise RuntimeError("private close token")

    close_failure = CloseFailureSdk(_account("chatgpt"))
    with pytest.raises(CodexOAuthProbeError) as close:
        CodexOAuthProbe(sdk_factory=lambda _config: close_failure).check(binding)
    assert "private" not in str(close.value)
    assert close.value.__context__ is None
    assert close_failure.close_count == 2

    malformed = FakeSdk()
    malformed.response = object()
    with pytest.raises(CodexOAuthProbeError) as protocol:
        CodexOAuthProbe(sdk_factory=lambda _config: malformed).check(binding)
    assert str(protocol.value) == "Codex authentication status is unavailable"
    assert malformed.close_count == 1


@pytest.mark.skipif(
    not os.environ.get("AGENTIC_MESH_TEST_CODEX_HOME"),
    reason="explicit external Codex home not supplied",
)
def test_current_external_codex_login_with_official_sdk() -> None:
    cache = Path(os.environ["AGENTIC_MESH_TEST_CODEX_HOME"])
    binding = CodexOAuthCacheResolver(
        {"current-login": cache},
        forbidden_roots=[ROOT],
    ).resolve("current-login")

    status = CodexOAuthProbe().check(binding)

    assert status.state is CodexOAuthState.AUTHENTICATED
    assert status.account_kind is CodexAccountKind.CHATGPT


def test_oauth_catalog_allows_the_v5_local_codex_provider() -> None:
    catalog = yaml.safe_load(
        (ROOT / "config" / "auth-methods.yaml").read_text(encoding="utf-8")
    )

    method = catalog["methods"]["codex_oauth_cache"]
    assert "codex-local" in method["applies_to"]
    assert method["env_vars"] == ["CODEX_HOME"]
    assert method["requires_mount_ref"] is True
    assert method["requires_secret_ref"] is False
