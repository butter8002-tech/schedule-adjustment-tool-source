from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from schedule_adjustment_tool.domain.models import Config, Participant
from schedule_adjustment_tool.ui.presentation import STATUS_LABELS as PROJECT_STATUS_LABELS


@dataclass(frozen=True)
class ManagerProjectSummary:
    project_id: str
    title: str
    status: str
    status_label: str
    participant_count: int
    target_count: int
    submitted_count: int
    pending_approval_count: int
    account_count: int
    answered_count: int
    manager_response_count: int
    role_unspecified_count: int
    individual_condition_count: int
    candidate_count: int
    confirmed: bool
    confirmed_candidate_number: object
    config_issue_count: int
    is_pristine: bool = False
    allow_edits_after_deadline: bool = False
    candidate_warning_count: int = 0


@dataclass(frozen=True)
class ManagerScreenContext:
    project_id: str
    config: Config
    participants: tuple[Participant, ...]
    candidates: tuple[dict[str, Any], ...]
    confirmed_candidate: dict[str, Any] | None
    summary: ManagerProjectSummary


def build_manager_project_summary_from_overview(
    project_id: str,
    config: Config,
    overview: dict[str, Any],
) -> ManagerProjectSummary:
    """Build the workflow summary from storage aggregates only."""

    confirmed = bool(overview.get("confirmed", False))
    candidate_count = int(overview.get("candidate_count", 0))
    return ManagerProjectSummary(
        project_id=project_id,
        title=config.title,
        status=config.status,
        status_label=PROJECT_STATUS_LABELS.get(config.status, config.status),
        participant_count=int(overview.get("participant_count", 0)),
        target_count=int(overview.get("target_count", 0)),
        submitted_count=int(overview.get("submitted_count", 0)),
        pending_approval_count=int(overview.get("pending_approval_count", 0)),
        account_count=int(overview.get("account_count", 0)),
        answered_count=int(overview.get("answered_count", 0)),
        manager_response_count=int(overview.get("manager_response_count", 0)),
        role_unspecified_count=int(overview.get("role_unspecified_count", 0)),
        individual_condition_count=int(
            overview.get("individual_condition_count", 0)
        ),
        candidate_count=candidate_count,
        candidate_warning_count=int(overview.get("candidate_warning_count", 0)),
        confirmed=confirmed,
        confirmed_candidate_number=(
            overview.get("confirmed_candidate_number", "-") if confirmed else "-"
        ),
        config_issue_count=int(
            overview.get("config_issue_count", len(config.validate()))
        ),
        allow_edits_after_deadline=bool(config.allow_edits_after_deadline),
        is_pristine=(
            config.status == "draft"
            and int(getattr(config, "_storage_version", 0) or 0) <= 1
            and candidate_count == 0
            and not confirmed
        ),
    )


def build_manager_screen_context(
    project_id: str,
    config: Config,
    participants: list[Participant],
    candidates: list[dict[str, Any]],
    confirmed: dict[str, Any] | None,
) -> ManagerScreenContext:
    target = [
        participant
        for participant in participants
        if participant.active and participant.approved
    ]
    condition_fields = (
        "required_university_count",
        "required_high_school_count",
        "total_extra_limit",
        "practice_participation_count",
    )
    summary = ManagerProjectSummary(
        project_id=project_id,
        title=config.title,
        status=config.status,
        status_label=PROJECT_STATUS_LABELS.get(config.status, config.status),
        participant_count=len(participants),
        target_count=len(target),
        submitted_count=sum(
            participant.input_status == "submitted" for participant in target
        ),
        pending_approval_count=sum(
            participant.active and not participant.approved
            for participant in participants
        ),
        account_count=sum(bool(participant.user_id) for participant in participants),
        answered_count=sum(
            participant.input_status in {"draft", "submitted"}
            for participant in target
        ),
        manager_response_count=sum(
            participant.response_source == "manager" for participant in target
        ),
        role_unspecified_count=sum(
            participant.is_practice_role_unspecified for participant in target
        ),
        individual_condition_count=sum(
            participant.practice_role_unspecified
            or any(getattr(participant, field) is not None for field in condition_fields)
            for participant in target
        ),
        candidate_count=len(candidates),
        candidate_warning_count=sum(
            not bool(candidate.get("metrics", {}).get("is_strict_candidate", True))
            for candidate in candidates
        ),
        confirmed=confirmed is not None,
        confirmed_candidate_number=(
            confirmed.get("candidate_number", "-") if confirmed else "-"
        ),
        config_issue_count=len(config.validate()),
        allow_edits_after_deadline=bool(config.allow_edits_after_deadline),
        is_pristine=(
            config.status == "draft"
            and int(getattr(config, "_storage_version", 0) or 0) <= 1
            and not candidates
            and confirmed is None
        ),
    )
    return ManagerScreenContext(
        project_id=project_id,
        config=config,
        participants=tuple(participants),
        candidates=tuple(candidates),
        confirmed_candidate=confirmed,
        summary=summary,
    )
