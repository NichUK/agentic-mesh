from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Mapping, Sequence

from agentic_mesh_v5.package_resolver import PackageReference
from agentic_mesh_v5.package_resolver import PackageResolutionError
from agentic_mesh_v5.package_resolver import resolve_packages


IDENTIFIER = re.compile(r"^[a-z][a-z0-9.-]*$")
PACKAGE_ID = re.compile(r"^[a-z][a-z0-9-]*$")
PROFILE_FIELDS = {
    "schema_version",
    "profile_id",
    "image",
    "capabilities",
    "mounts",
    "credentials",
    "health",
    "resources",
}
PROFILE_OPTIONAL_FIELDS = {"launch_policy"}


class ToolProfileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ImageSpec:
    repository: str
    tag: str
    platform: str


@dataclass(frozen=True, slots=True)
class Capability:
    id: str
    required: bool


@dataclass(frozen=True, slots=True)
class MountRequirement:
    id: str
    source: str
    target: PurePosixPath
    access: str
    required: bool


@dataclass(frozen=True, slots=True)
class CredentialRequirement:
    id: str
    kind: str
    delivery: str
    target: str
    required: bool


@dataclass(frozen=True, slots=True)
class HealthCheck:
    command: tuple[str, ...]
    interval_seconds: int
    timeout_seconds: int
    failure_threshold: int


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    cpu_millis: int
    memory_mb: int
    ephemeral_storage_mb: int


@dataclass(frozen=True, slots=True)
class LaunchPolicy:
    normal_routing: bool
    allowed_launchers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolProfile:
    reference: str
    configuration_digest: str
    profile_id: str
    image: ImageSpec
    capabilities: tuple[Capability, ...]
    mounts: tuple[MountRequirement, ...]
    credentials: tuple[CredentialRequirement, ...]
    health: HealthCheck
    resources: ResourceLimits
    launch_policy: LaunchPolicy

    @property
    def capability_ids(self) -> frozenset[str]:
        return frozenset(item.id for item in self.capabilities)

    def missing_capabilities(self, required: Sequence[str]) -> tuple[str, ...]:
        identifiers = tuple(
            _identifier(item, "required capability id") for item in required
        )
        return tuple(sorted(set(identifiers) - self.capability_ids))

    def require_capabilities(self, required: Sequence[str]) -> None:
        missing = self.missing_capabilities(required)
        if missing:
            raise ToolProfileError(
                f"tool profile {self.profile_id} lacks capabilities: {list(missing)}"
            )

    def authorize_launch(
        self, launcher: str, *, via_normal_routing: bool
    ) -> None:
        launcher_id = _package_id(launcher, "launcher")
        if via_normal_routing and not self.launch_policy.normal_routing:
            raise ToolProfileError(
                f"tool profile {self.profile_id} is excluded from normal routing"
            )
        allowed = self.launch_policy.allowed_launchers
        if allowed and launcher_id not in allowed:
            raise ToolProfileError(
                f"launcher {launcher_id} is not authorized for tool profile "
                f"{self.profile_id}"
            )


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ToolProfileError(f"{label} must be an object")
    return value


def _fields(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ToolProfileError(f"{label} fields do not match the tool-profile contract")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolProfileError(f"{label} must be a non-empty string")
    if value != value.strip():
        raise ToolProfileError(f"{label} must not have surrounding whitespace")
    return value


def _identifier(value: object, label: str) -> str:
    result = _string(value, label)
    if IDENTIFIER.fullmatch(result) is None:
        raise ToolProfileError(f"{label} is invalid")
    return result


def _package_id(value: object, label: str) -> str:
    result = _string(value, label)
    if PACKAGE_ID.fullmatch(result) is None:
        raise ToolProfileError(f"{label} is invalid")
    return result


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ToolProfileError(f"{label} must be a positive integer")
    return value


def _items(value: object, label: str, *, nonempty: bool = False) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ToolProfileError(f"{label} must be a{' non-empty' if nonempty else ''} list")
    return [_object(item, f"{label} item") for item in value]


def _unique(values: Sequence[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ToolProfileError(f"{label} must be unique")


def _required_boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ToolProfileError(f"{label} must be boolean")
    return value


def _profile_fields(value: Mapping[str, object]) -> None:
    keys = set(value)
    missing = sorted(PROFILE_FIELDS - keys)
    unexpected = sorted(keys - PROFILE_FIELDS - PROFILE_OPTIONAL_FIELDS)
    if missing or unexpected:
        raise ToolProfileError(
            "tool_profile fields do not match the tool-profile contract"
        )


def _parse_profile(reference: str, digest: str, value: object) -> ToolProfile:
    package_reference = PackageReference.parse(reference)
    profile = _object(value, "tool_profile")
    _profile_fields(profile)
    profile_id = _package_id(profile.get("profile_id"), "profile_id")
    if profile.get("schema_version") != 1:
        raise ToolProfileError("unsupported tool-profile schema_version")
    if profile_id != package_reference.package_id:
        raise ToolProfileError("tool-profile identity does not match its package reference")

    image_value = _object(profile.get("image"), "image")
    _fields(image_value, {"repository", "tag", "platform"}, "image")
    repository = _string(image_value.get("repository"), "image.repository")
    if "://" in repository:
        raise ToolProfileError("image.repository must not include a URL scheme")
    if "@" in repository:
        raise ToolProfileError(
            "image.repository must not contain @; digest references are unsupported "
            "because image.tag is separate"
        )
    image = ImageSpec(
        repository,
        _string(image_value.get("tag"), "image.tag"),
        _string(image_value.get("platform"), "image.platform"),
    )

    capabilities: list[Capability] = []
    for item in _items(profile.get("capabilities"), "capabilities", nonempty=True):
        _fields(item, {"id", "required"}, "capability")
        capabilities.append(
            Capability(
                _identifier(item.get("id"), "capability.id"),
                _required_boolean(item.get("required"), "capability.required"),
            )
        )
    _unique([item.id for item in capabilities], "capability ids")

    mounts: list[MountRequirement] = []
    for item in _items(profile.get("mounts"), "mounts"):
        _fields(item, {"id", "source", "target", "access", "required"}, "mount")
        raw_target = _string(item.get("target"), "mount.target")
        target = PurePosixPath(raw_target)
        if not target.is_absolute() or ".." in target.parts:
            raise ToolProfileError("mount.target must be an absolute contained POSIX path")
        access = _string(item.get("access"), "mount.access")
        if access not in {"read-only", "read-write"}:
            raise ToolProfileError("mount.access is invalid")
        mounts.append(
            MountRequirement(
                _package_id(item.get("id"), "mount.id"),
                _string(item.get("source"), "mount.source"),
                target,
                access,
                _required_boolean(item.get("required"), "mount.required"),
            )
        )
    _unique([item.id for item in mounts], "mount ids")
    _unique([str(item.target) for item in mounts], "mount targets")

    credentials: list[CredentialRequirement] = []
    for item in _items(profile.get("credentials"), "credentials"):
        _fields(item, {"id", "kind", "delivery", "target", "required"}, "credential")
        delivery = _string(item.get("delivery"), "credential.delivery")
        if delivery not in {"mount", "environment", "provider"}:
            raise ToolProfileError("credential.delivery is invalid")
        target = _string(item.get("target"), "credential.target")
        credential_target = PurePosixPath(target)
        if delivery == "mount" and (
            not credential_target.is_absolute() or ".." in credential_target.parts
        ):
            raise ToolProfileError(
                "mounted credential target must be an absolute contained POSIX path"
            )
        credentials.append(
            CredentialRequirement(
                _package_id(item.get("id"), "credential.id"),
                _string(item.get("kind"), "credential.kind"),
                delivery,
                target,
                _required_boolean(item.get("required"), "credential.required"),
            )
        )
    _unique([item.id for item in credentials], "credential ids")

    health_value = _object(profile.get("health"), "health")
    _fields(
        health_value,
        {"command", "interval_seconds", "timeout_seconds", "failure_threshold"},
        "health",
    )
    command_value = health_value.get("command")
    if not isinstance(command_value, list) or not command_value:
        raise ToolProfileError("health.command must be a non-empty string list")
    command = tuple(_string(item, "health.command item") for item in command_value)
    interval = _positive_integer(health_value.get("interval_seconds"), "health.interval_seconds")
    timeout = _positive_integer(health_value.get("timeout_seconds"), "health.timeout_seconds")
    if timeout > interval:
        raise ToolProfileError("health timeout cannot exceed interval")
    health = HealthCheck(
        command,
        interval,
        timeout,
        _positive_integer(health_value.get("failure_threshold"), "health.failure_threshold"),
    )

    resource_value = _object(profile.get("resources"), "resources")
    _fields(
        resource_value,
        {"cpu_millis", "memory_mb", "ephemeral_storage_mb"},
        "resources",
    )
    resources = ResourceLimits(
        _positive_integer(resource_value.get("cpu_millis"), "resources.cpu_millis"),
        _positive_integer(resource_value.get("memory_mb"), "resources.memory_mb"),
        _positive_integer(
            resource_value.get("ephemeral_storage_mb"),
            "resources.ephemeral_storage_mb",
        ),
    )
    launch_value = profile.get("launch_policy")
    if launch_value is None:
        launch_policy = LaunchPolicy(True, ())
    else:
        launch = _object(launch_value, "launch_policy")
        _fields(
            launch,
            {"normal_routing", "allowed_launchers"},
            "launch_policy",
        )
        normal_routing = _required_boolean(
            launch.get("normal_routing"), "launch_policy.normal_routing"
        )
        if normal_routing:
            raise ToolProfileError(
                "explicit launch_policy is only for profiles excluded from normal routing"
            )
        raw_launchers = launch.get("allowed_launchers")
        if not isinstance(raw_launchers, list) or not raw_launchers:
            raise ToolProfileError(
                "launch_policy.allowed_launchers must be a non-empty list"
            )
        allowed_launchers = tuple(
            _package_id(item, "launch_policy.allowed_launcher")
            for item in raw_launchers
        )
        _unique(allowed_launchers, "launch_policy allowed launchers")
        launch_policy = LaunchPolicy(False, allowed_launchers)
    return ToolProfile(
        reference,
        digest,
        profile_id,
        image,
        tuple(capabilities),
        tuple(mounts),
        tuple(credentials),
        health,
        resources,
        launch_policy,
    )


class ToolProfileRegistry:
    def __init__(self, configuration_root: Path) -> None:
        self.configuration_root = configuration_root

    def load(self, reference: str) -> ToolProfile:
        try:
            parsed = PackageReference.parse(reference)
            if parsed.kind != "tool-profile":
                raise ToolProfileError("tool-profile registry requires a tool-profile reference")
            resolved = resolve_packages(self.configuration_root, [reference])
        except PackageResolutionError as exc:
            raise ToolProfileError(str(exc)) from exc
        if set(resolved.settings) != {"tool_profile"}:
            raise ToolProfileError("resolved package must contain exactly one tool_profile object")
        return _parse_profile(reference, resolved.digest, resolved.settings["tool_profile"])

    def load_for_launch(
        self,
        reference: str,
        *,
        launcher: str,
        via_normal_routing: bool,
    ) -> ToolProfile:
        profile = self.load(reference)
        profile.authorize_launch(
            launcher,
            via_normal_routing=via_normal_routing,
        )
        return profile
