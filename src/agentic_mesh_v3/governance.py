from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Iterable
from typing import Mapping


@dataclass(frozen=True)
class RaciAssignment:
    phase: str
    accountable: str
    responsible: tuple[str, ...]
    consulted: tuple[str, ...] = ()
    informed: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.phase:
            raise ValueError("phase is required")
        if not self.accountable:
            raise ValueError(f"{self.phase}: one accountable role is required")
        if not self.responsible:
            raise ValueError(f"{self.phase}: at least one responsible role is required")
        if self.accountable in self.informed:
            raise ValueError(f"{self.phase}: accountable role cannot be only informed")


@dataclass(frozen=True)
class RaciMatrix:
    assignments: tuple[RaciAssignment, ...]

    def validate(self) -> None:
        seen: set[str] = set()
        for assignment in self.assignments:
            assignment.validate()
            if assignment.phase in seen:
                raise ValueError(f"duplicate RACI phase: {assignment.phase}")
            seen.add(assignment.phase)

    def for_phase(self, phase: str) -> RaciAssignment:
        for assignment in self.assignments:
            if assignment.phase == phase:
                return assignment
        raise KeyError(f"unknown RACI phase: {phase}")


@dataclass(frozen=True)
class GovernanceContext:
    work_item_id: str
    phase: str
    accountable_role: str
    responsible_roles: tuple[str, ...]
    consulted_roles: tuple[str, ...] = ()
    informed_roles: tuple[str, ...] = ()
    sponsor_decision_points: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    consultation_exceptions: tuple[str, ...] = ()

    @classmethod
    def from_assignment(
        cls,
        *,
        work_item_id: str,
        assignment: RaciAssignment,
        sponsor_decision_points: tuple[str, ...] = (),
        required_evidence: tuple[str, ...] = (),
    ) -> GovernanceContext:
        return cls(
            work_item_id=work_item_id,
            phase=assignment.phase,
            accountable_role=assignment.accountable,
            responsible_roles=assignment.responsible,
            consulted_roles=assignment.consulted,
            informed_roles=assignment.informed,
            sponsor_decision_points=sponsor_decision_points,
            required_evidence=required_evidence,
        )

    def required_consultations(self) -> tuple[str, ...]:
        excepted = set(self.consultation_exceptions)
        return tuple(role for role in self.consulted_roles if role not in excepted)

    def handoff_requirements(self) -> dict[str, object]:
        return {
            "work_item_id": self.work_item_id,
            "phase": self.phase,
            "accountable_role": self.accountable_role,
            "responsible_roles": list(self.responsible_roles),
            "consulted_roles": list(self.consulted_roles),
            "informed_roles": list(self.informed_roles),
            "sponsor_decision_points": list(self.sponsor_decision_points),
            "required_evidence": list(self.required_evidence),
            "consultation_exceptions": list(self.consultation_exceptions),
        }


@dataclass(frozen=True)
class GovernanceChecklist:
    """Agent-facing evidence checklist for a work item's governance context."""

    missing_consultations: tuple[str, ...] = ()
    missing_informed_updates: tuple[str, ...] = ()
    pending_sponsor_decisions: tuple[str, ...] = ()
    recorded_exceptions: tuple[str, ...] = ()

    @property
    def is_satisfied(self) -> bool:
        return not (
            self.missing_consultations
            or self.missing_informed_updates
            or self.pending_sponsor_decisions
        )

    def as_prompt_section(self) -> str:
        lines = ["<governance-checklist>"]
        if self.is_satisfied:
            lines.append("- Governance checklist is currently satisfied.")
        else:
            for role in self.missing_consultations:
                lines.append(f"- Missing consultation evidence for `{role}`.")
            for role in self.missing_informed_updates:
                lines.append(f"- Missing informed-update evidence for `{role}`.")
            for decision in self.pending_sponsor_decisions:
                lines.append(f"- Pending sponsor/stakeholder decision `{decision}`.")
        if self.recorded_exceptions:
            lines.append("- Recorded governance exceptions:")
            lines.extend(f"  - {exception}" for exception in self.recorded_exceptions)
        lines.append("</governance-checklist>")
        return "\n".join(lines)


def evaluate_governance_checklist(
    context: GovernanceContext,
    *,
    governance_records: Iterable[object] = (),
    approvals: Iterable[object] = (),
) -> GovernanceChecklist:
    """Return governance evidence still missing before a phase can close.

    This helper does not advance state or decide whether exceptions are valid.
    It gives role agents an explicit checklist of the consultations, informed
    updates, and sponsor decisions they still need to resolve through tools.
    """

    records = tuple(governance_records)
    exception_targets = {
        str(target)
        for record in records
        if _field(record, "record_type") == "governance.record_exception"
        for target in (_field(record, "target_ref"),)
        if target
    }
    recorded_exceptions = tuple(
        str(_field(record, "summary") or _field(record, "target_ref") or "governance exception recorded")
        for record in records
        if _field(record, "record_type") == "governance.record_exception"
    )
    consulted_exceptions = set(context.consultation_exceptions) | exception_targets
    missing_consultations = tuple(
        role
        for role in context.consulted_roles
        if role not in consulted_exceptions
        and not _has_record(records, record_type="consult.request", target_ref=role)
    )
    missing_informed_updates = tuple(
        role
        for role in context.informed_roles
        if role not in exception_targets
        and not _has_record(records, record_type="informed.update", target_ref=role)
    )
    pending_sponsor_decisions = tuple(
        decision
        for decision in context.sponsor_decision_points
        if not _has_resolved_approval(approvals, decision)
    )
    return GovernanceChecklist(
        missing_consultations=missing_consultations,
        missing_informed_updates=missing_informed_updates,
        pending_sponsor_decisions=pending_sponsor_decisions,
        recorded_exceptions=recorded_exceptions,
    )


@dataclass(frozen=True)
class GovernanceInstructionSet:
    rules: tuple[str, ...] = field(
        default=(
            "Consult every role marked C before completing the current phase, unless the accountable role records a justified governance exception.",
            "Inform every role marked I when phase state changes, a major decision is made, a blocker appears, or a release occurs.",
            "Ask the sponsor or stakeholder when scope, priority, acceptance criteria, user-visible behavior, release risk, cost, compliance, security posture, or delivery commitment changes.",
            "Consult Prompt Engineer when a slice changes prompt components, role instructions, safe-output tool guidance, context loading, memory instructions, or prompt-driven behaviour.",
            "Do not continue through unresolved governance questions silently; ask, record the question, and wait or proceed only with an explicit documented assumption.",
            "Resolve role disagreements through written review loops first, then request mediation from Project Manager or sponsor after the configured loop limit.",
            "Write governance evidence into the work-item index and durable decision or risk registers where appropriate.",
        )
    )

    def as_prompt_section(self) -> str:
        lines = ["<governance-instructions>"]
        lines.extend(f"- {rule}" for rule in self.rules)
        lines.append("</governance-instructions>")
        return "\n".join(lines)


def _has_record(records: tuple[object, ...], *, record_type: str, target_ref: str) -> bool:
    for record in records:
        if _field(record, "record_type") == record_type and _field(record, "target_ref") == target_ref:
            status = str(_field(record, "status") or "").casefold()
            if status not in {"canceled", "failed", "rejected"}:
                return True
    return False


def _has_resolved_approval(approvals: Iterable[object], decision_point: str) -> bool:
    decision = decision_point.casefold()
    for approval in approvals:
        status = str(_field(approval, "status") or "").casefold()
        if status not in {"approved", "changes_requested", "rejected"}:
            continue
        approval_id = str(_field(approval, "approval_id") or "").casefold()
        question = str(_field(approval, "question") or "").casefold()
        if approval_id == decision or decision in question:
            return True
    return False


def _field(record: object, name: str) -> object | None:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


DEFAULT_SDLC_RACI = RaciMatrix(
    assignments=(
        RaciAssignment(
            phase="requirements",
            accountable="project-manager",
            responsible=("business-analyst",),
            consulted=("product-manager", "solution-architect", "research-analyst", "stakeholders"),
            informed=("delivery-manager", "engineering", "qa-engineer", "platform-engineer"),
        ),
        RaciAssignment(
            phase="feasibility-and-planning",
            accountable="project-manager",
            responsible=("delivery-manager", "product-manager"),
            consulted=(
                "business-analyst",
                "solution-architect",
                "enterprise-architect",
                "platform-engineer",
                "security-architect",
                "stakeholders",
            ),
            informed=("engineering", "qa-engineer", "release-manager"),
        ),
        RaciAssignment(
            phase="enterprise-and-architecture-alignment",
            accountable="enterprise-architect",
            responsible=("solution-architect",),
            consulted=("product-manager", "business-analyst", "security-architect", "platform-engineer"),
            informed=("project-manager", "delivery-manager", "stakeholders"),
        ),
        RaciAssignment(
            phase="system-design",
            accountable="solution-architect",
            responsible=("solution-architect", "ux-designer"),
            consulted=("security-architect", "platform-engineer", "engineering", "qa-engineer", "product-manager"),
            informed=("project-manager", "delivery-manager", "stakeholders"),
        ),
        RaciAssignment(
            phase="development",
            accountable="engineering",
            responsible=("engineering",),
            consulted=(
                "product-manager",
                "solution-architect",
                "security-architect",
                "qa-engineer",
                "platform-engineer",
            ),
            informed=("project-manager", "delivery-manager", "stakeholders"),
        ),
        RaciAssignment(
            phase="testing-and-qa",
            accountable="qa-engineer",
            responsible=("qa-engineer", "engineering"),
            consulted=("product-manager", "ux-designer", "security-architect", "platform-engineer"),
            informed=("project-manager", "delivery-manager", "stakeholders"),
        ),
        RaciAssignment(
            phase="deployment",
            accountable="release-manager",
            responsible=("platform-engineer", "engineering"),
            consulted=("qa-engineer", "security-architect", "solution-architect"),
            informed=("project-manager", "delivery-manager", "product-manager", "stakeholders"),
        ),
        RaciAssignment(
            phase="maintenance-and-support",
            accountable="platform-engineer",
            responsible=("platform-engineer", "engineering", "release-manager"),
            consulted=("qa-engineer", "product-manager", "security-architect"),
            informed=("project-manager", "delivery-manager", "stakeholders"),
        ),
        RaciAssignment(
            phase="project-closure",
            accountable="project-manager",
            responsible=("project-manager", "delivery-manager"),
            consulted=("product-manager", "release-manager", "technical-writer", "stakeholders"),
            informed=("business-analyst", "solution-architect", "engineering", "qa-engineer", "platform-engineer"),
        ),
    )
)
