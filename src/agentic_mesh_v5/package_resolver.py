from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from agentic_mesh_v5.config_repository import validate_mounted_repository


KINDS = (
    "system",
    "policy",
    "fragment",
    "role",
    "flow",
    "tool-profile",
    "project-override",
)
KIND_PRECEDENCE = {kind: index for index, kind in enumerate(KINDS)}
REFERENCE = re.compile(
    r"^(?P<kind>[a-z][a-z0-9-]*)/(?P<package_id>[a-z][a-z0-9-]*)@"
    r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$"
)


class PackageResolutionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PackageReference:
    kind: str
    package_id: str
    version: str

    @classmethod
    def parse(cls, value: str) -> PackageReference:
        match = REFERENCE.fullmatch(value)
        if match is None or match.group("kind") not in KIND_PRECEDENCE:
            raise PackageResolutionError(f"invalid package reference: {value}")
        return cls(**match.groupdict())

    def __str__(self) -> str:
        return f"{self.kind}/{self.package_id}@{self.version}"


@dataclass(frozen=True, slots=True)
class ContentProvenance:
    package: str
    path: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {"package": self.package, "path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class TextSection:
    package: str
    path: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"package": self.package, "path": self.path, "text": self.text}


@dataclass(frozen=True, slots=True)
class ResolvedConfiguration:
    digest: str
    packages: tuple[str, ...]
    settings: Mapping[str, Any]
    text_sections: tuple[TextSection, ...]
    provenance: tuple[ContentProvenance, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "digest": self.digest,
            "packages": list(self.packages),
            "settings": self.settings,
            "text_sections": [section.to_dict() for section in self.text_sections],
            "provenance": [source.to_dict() for source in self.provenance],
        }


@dataclass(frozen=True, slots=True)
class _Content:
    relative_path: str
    text: str
    json_value: Mapping[str, Any] | None
    sha256: str


@dataclass(frozen=True, slots=True)
class _Package:
    reference: PackageReference
    dependencies: tuple[PackageReference, ...]
    manifest_path: str
    manifest_sha256: str
    content: tuple[_Content, ...]


def _reference_key(reference: PackageReference) -> tuple[int, str]:
    return KIND_PRECEDENCE[reference.kind], str(reference)


def _load_package(root: Path, reference: PackageReference) -> _Package:
    package_root = root / "packages" / reference.kind / reference.package_id / reference.version
    manifest_path = package_root / "package.json"
    try:
        resolved_package_root = package_root.resolve(strict=True)
        resolved_package_root.relative_to(root)
        resolved_manifest = manifest_path.resolve(strict=True)
        resolved_manifest.relative_to(resolved_package_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PackageResolutionError(f"package not found: {reference}") from exc
    if not resolved_manifest.is_file():
        raise PackageResolutionError(f"package not found: {reference}")
    try:
        manifest_text = (
            resolved_manifest.read_text(encoding="utf-8")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
        manifest = json.loads(manifest_text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageResolutionError(f"invalid manifest for {reference}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PackageResolutionError(f"manifest for {reference} must be a JSON object")
    required = {"schema_version", "id", "kind", "version", "content"}
    allowed = required | {"dependencies", "description"}
    missing = sorted(required - manifest.keys())
    unexpected = sorted(manifest.keys() - allowed)
    if missing or unexpected:
        raise PackageResolutionError(
            f"manifest fields do not match schema for {reference}: "
            f"missing={missing}, unexpected={unexpected}"
        )
    if "description" in manifest and not isinstance(manifest["description"], str):
        raise PackageResolutionError(f"description for {reference} must be a string")
    identity = (manifest.get("kind"), manifest.get("id"), manifest.get("version"))
    if manifest.get("schema_version") != 1 or identity != (
        reference.kind,
        reference.package_id,
        reference.version,
    ):
        raise PackageResolutionError(f"manifest identity does not match {reference}")
    dependency_values = manifest.get("dependencies", [])
    if not isinstance(dependency_values, list) or not all(
        isinstance(value, str) for value in dependency_values
    ):
        raise PackageResolutionError(f"dependencies for {reference} must be strings")
    dependencies = tuple(PackageReference.parse(value) for value in dependency_values)
    content_values = manifest.get("content")
    if not isinstance(content_values, list) or not content_values or not all(
        isinstance(value, str) for value in content_values
    ):
        raise PackageResolutionError(f"content for {reference} must be a non-empty string list")

    content: list[_Content] = []
    seen_paths: set[str] = set()
    for value in content_values:
        content_path = package_root / value
        try:
            resolved_content = content_path.resolve(strict=True)
            resolved_content.relative_to(resolved_package_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise PackageResolutionError(f"invalid content path for {reference}: {value}") from exc
        if not resolved_content.is_file() or value in seen_paths:
            raise PackageResolutionError(f"invalid content path for {reference}: {value}")
        seen_paths.add(value)
        try:
            payload = resolved_content.read_bytes()
            text = payload.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except (OSError, UnicodeError) as exc:
            raise PackageResolutionError(f"unreadable content for {reference}: {value}") from exc
        json_value: Mapping[str, Any] | None = None
        if resolved_content.suffix.lower() == ".json":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise PackageResolutionError(f"invalid JSON content for {reference}: {value}") from exc
            if not isinstance(parsed, dict):
                raise PackageResolutionError(
                    f"JSON content for {reference} must be an object: {value}"
                )
            json_value = parsed
        content.append(
            _Content(
                relative_path=resolved_content.relative_to(root).as_posix(),
                text=text,
                json_value=json_value,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )
    return _Package(
        reference,
        dependencies,
        manifest_path.relative_to(root).as_posix(),
        hashlib.sha256(manifest_text.encode("utf-8")).hexdigest(),
        tuple(content),
    )


def _merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged


def resolve_packages(root: Path, references: Sequence[str]) -> ResolvedConfiguration:
    if not references:
        raise PackageResolutionError("at least one package reference is required")
    root = root.resolve()
    try:
        validate_mounted_repository(root)
    except ValueError as exc:
        raise PackageResolutionError(str(exc)) from exc

    roots = sorted({PackageReference.parse(value) for value in references}, key=_reference_key)
    packages: dict[PackageReference, _Package] = {}
    visiting: list[PackageReference] = []

    def visit(reference: PackageReference) -> None:
        if reference in packages:
            return
        if reference in visiting:
            cycle = visiting[visiting.index(reference) :] + [reference]
            raise PackageResolutionError(
                "package dependency cycle: " + " -> ".join(str(item) for item in cycle)
            )
        visiting.append(reference)
        package = _load_package(root, reference)
        for dependency in sorted(set(package.dependencies), key=_reference_key):
            visit(dependency)
        visiting.pop()
        packages[reference] = package

    for reference in roots:
        visit(reference)

    ordered = sorted(packages.values(), key=lambda package: _reference_key(package.reference))
    settings: dict[str, Any] = {}
    text_sections: list[TextSection] = []
    provenance: list[ContentProvenance] = []
    for package in ordered:
        package_name = str(package.reference)
        provenance.append(
            ContentProvenance(
                package_name, package.manifest_path, package.manifest_sha256
            )
        )
        for content in package.content:
            provenance.append(
                ContentProvenance(package_name, content.relative_path, content.sha256)
            )
            if content.json_value is None:
                text_sections.append(
                    TextSection(package_name, content.relative_path, content.text)
                )
            else:
                settings = _merge(settings, content.json_value)

    unsigned: dict[str, object] = {
        "schema_version": 1,
        "packages": [str(package.reference) for package in ordered],
        "settings": settings,
        "text_sections": [section.to_dict() for section in text_sections],
        "provenance": [source.to_dict() for source in provenance],
    }
    canonical = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ResolvedConfiguration(
        digest=digest,
        packages=tuple(unsigned["packages"]),
        settings=settings,
        text_sections=tuple(text_sections),
        provenance=tuple(provenance),
    )
