from __future__ import annotations

from dataclasses import dataclass

from agentic_mesh.models import DocumentAccountability


@dataclass(frozen=True)
class DocumentLifecycleEvent:
    event_type: str
    path: str
    owner_role: str
    contributor_role: str
    review_required: bool


class DocumentLifecyclePlanner:
    def __init__(self, documents: dict[str, DocumentAccountability]) -> None:
        self.documents = documents

    def contribution_events(
        self,
        path: str,
        contributor_role: str,
    ) -> list[DocumentLifecycleEvent]:
        accountability = self.documents[path]
        if (
            contributor_role != accountability.owner_role
            and contributor_role not in accountability.contributing_roles
        ):
            raise ValueError(
                f"Role {contributor_role} is not configured to contribute to {path}"
            )

        events = [
            DocumentLifecycleEvent(
                event_type="document.contribution_added",
                path=path,
                owner_role=accountability.owner_role,
                contributor_role=contributor_role,
                review_required=accountability.review_on_contribution,
            )
        ]
        if (
            accountability.review_on_contribution
            and contributor_role != accountability.owner_role
        ):
            events.append(
                DocumentLifecycleEvent(
                    event_type="document.owner_review_requested",
                    path=path,
                    owner_role=accountability.owner_role,
                    contributor_role=contributor_role,
                    review_required=True,
                )
            )
        return events
