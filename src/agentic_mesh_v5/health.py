from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Literal

from agentic_mesh_v5 import __version__
from agentic_mesh_v5.database import DatabaseError, MigrationError, MigrationRunner
from agentic_mesh_v5.telemetry import Telemetry


HealthStatus = Literal["ok", "degraded"]
DependencyStatus = Literal["ok", "degraded", "unavailable"]


@dataclass(frozen=True)
class DependencyHealth:
    name: str
    status: DependencyStatus
    reason: str


@dataclass(frozen=True)
class HealthReport:
    runtime: str
    version: str
    kind: Literal["liveness", "readiness"]
    status: HealthStatus
    checked_at: str
    schema_version: int | None
    available_schema_version: int | None
    dependencies: tuple[DependencyHealth, ...]

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["dependencies"] = [asdict(item) for item in self.dependencies]
        return result


class HealthReporter:
    def __init__(self, database_url: str, telemetry: Telemetry) -> None:
        self._runner = MigrationRunner(database_url)
        self._telemetry = telemetry

    def liveness(self) -> HealthReport:
        return HealthReport(
            runtime="agentic-mesh-v5",
            version=__version__,
            kind="liveness",
            status="ok",
            checked_at=_now(),
            schema_version=None,
            available_schema_version=None,
            dependencies=(),
        )

    def readiness(self) -> HealthReport:
        started_at = monotonic()
        try:
            migration = self._runner.status()
        except MigrationError:
            dependency = DependencyHealth(
                name="postgres", status="degraded", reason="schema_invalid"
            )
            report = HealthReport(
                runtime="agentic-mesh-v5",
                version=__version__,
                kind="readiness",
                status="degraded",
                checked_at=_now(),
                schema_version=None,
                available_schema_version=None,
                dependencies=(dependency,),
            )
        except DatabaseError:
            dependency = DependencyHealth(
                name="postgres", status="unavailable", reason="connection_failed"
            )
            report = HealthReport(
                runtime="agentic-mesh-v5",
                version=__version__,
                kind="readiness",
                status="degraded",
                checked_at=_now(),
                schema_version=None,
                available_schema_version=None,
                dependencies=(dependency,),
            )
        else:
            pending = bool(migration.pending_versions)
            dependency = DependencyHealth(
                name="postgres",
                status="degraded" if pending else "ok",
                reason="pending_migrations" if pending else "schema_current",
            )
            report = HealthReport(
                runtime="agentic-mesh-v5",
                version=__version__,
                kind="readiness",
                status="degraded" if pending else "ok",
                checked_at=_now(),
                schema_version=migration.current_version,
                available_schema_version=migration.available_version,
                dependencies=(dependency,),
            )
        self._telemetry.record_health(
            dependency="postgres",
            status=report.dependencies[0].status,
            duration=monotonic() - started_at,
        )
        return report


def _now() -> str:
    return datetime.now(UTC).isoformat()
