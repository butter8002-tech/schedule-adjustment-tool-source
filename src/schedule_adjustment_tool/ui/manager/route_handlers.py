"""Bind the manager workflow routes to the application's business screens.

This module owns route-to-screen wiring only.  The screen implementations stay
outside the shell so a missing operation cannot silently become a placeholder
screen, while storage and scheduling contracts remain independent of the UI.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from schedule_adjustment_tool.domain.models import Config, Participant

RouteHandler = Callable[[], None]
ConfirmedSchedule = dict[str, Any] | None
ProjectScreenRenderer = Callable[
    [str, Config, list[Participant], ConfirmedSchedule], None
]
CandidateScreenRenderer = Callable[[str, Config, list[Participant]], None]


@dataclass(frozen=True)
class ManagerScreenRenderers:
    """Business screens used by the schedule-manager workflow.

    Keeping these dependencies explicit prevents the workflow shell from
    importing storage-facing application code and makes every formal route
    auditable from one mapping.
    """

    project_basic: ProjectScreenRenderer
    response_window: ProjectScreenRenderer
    group_settings: ProjectScreenRenderer
    participant_roster: Callable[[str, Config, list[Participant]], None]
    participant_membership: ProjectScreenRenderer
    participant_accounts: Callable[..., None]
    response_status: Callable[..., None]
    role_and_participation: ProjectScreenRenderer
    participant_individual_conditions: ProjectScreenRenderer
    evaluation_preferences: ProjectScreenRenderer
    candidate_search_settings: Callable[..., None]
    candidates: Callable[..., None]
    candidate_list: CandidateScreenRenderer
    candidate_adjustment: CandidateScreenRenderer
    candidate_publish: ProjectScreenRenderer
    current_schedule: ProjectScreenRenderer
    schedule_amendments: ProjectScreenRenderer
    project_exports: CandidateScreenRenderer
    project_access: Callable[[str, Config], None]


def build_manager_route_handlers(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: ConfirmedSchedule,
    *,
    renderers: ManagerScreenRenderers,
    participants_loader: Callable[[], list[Participant]] | None = None,
    confirmed_loader: Callable[[], ConfirmedSchedule] | None = None,
) -> dict[str, RouteHandler]:
    """Return handlers for every non-home manager route.

    ``render_manager_shell`` validates this mapping against the route catalog
    before rendering, so a new route must be deliberately connected here.
    """

    get_participants = participants_loader or (lambda: participants)
    get_confirmed = confirmed_loader or (lambda: confirmed)
    return {
        "project_setup/basic": lambda: renderers.project_basic(
            project_id,
            config,
            get_participants(),
            get_confirmed(),
        ),
        "project_setup/response_window": lambda: renderers.response_window(
            project_id,
            config,
            get_participants(),
            get_confirmed(),
        ),
        "participants/groups": lambda: renderers.group_settings(
            project_id,
            config,
            get_participants(),
            get_confirmed(),
        ),
        "participants/roster": lambda: renderers.participant_roster(
            project_id,
            config,
            get_participants(),
        ),
        "participants/membership": lambda: renderers.participant_membership(
            project_id,
            config,
            get_participants(),
            get_confirmed(),
        ),
        "participants/accounts": lambda: renderers.participant_accounts(
            project_id,
            get_participants(),
            heading="参加者アカウント",
            account_source="スケジュール担当者",
        ),
        "responses/status": lambda: renderers.response_status(
            config,
            get_participants(),
            section="status",
            show_header=False,
        ),
        "responses/content": lambda: renderers.response_status(
            config,
            get_participants(),
            section="content",
            show_header=False,
        ),
        "responses/proxy": lambda: renderers.response_status(
            config,
            get_participants(),
            section="proxy",
            show_header=False,
        ),
        "conditions/feasibility": lambda: renderers.role_and_participation(
            project_id,
            config,
            get_participants(),
            get_confirmed(),
        ),
        "conditions/individual": lambda: (
            renderers.participant_individual_conditions(
                project_id,
                config,
                get_participants(),
                get_confirmed(),
            )
        ),
        "conditions/evaluation": lambda: renderers.evaluation_preferences(
            project_id,
            config,
            get_participants(),
            get_confirmed(),
        ),
        "conditions/advanced": lambda: renderers.candidate_search_settings(
            project_id,
            config,
            expanded=True,
        ),
        "candidates/create": lambda: renderers.candidates(
            project_id,
            config,
            get_participants(),
            get_confirmed(),
            creation_only=True,
        ),
        "candidates/list": lambda: renderers.candidate_list(
            project_id,
            config,
            get_participants(),
        ),
        "candidates/adjust": lambda: renderers.candidate_adjustment(
            project_id,
            config,
            get_participants(),
        ),
        "publish/review": lambda: renderers.candidate_publish(
            project_id,
            config,
            get_participants(),
            get_confirmed(),
        ),
        "post_publish/current": lambda: renderers.current_schedule(
            project_id,
            config,
            get_participants(),
            get_confirmed(),
        ),
        "post_publish/amendments": lambda: renderers.schedule_amendments(
            project_id,
            config,
            get_participants(),
            get_confirmed(),
        ),
        "utility/export": lambda: renderers.project_exports(
            project_id,
            config,
            get_participants(),
        ),
        "utility/access": lambda: renderers.project_access(project_id, config),
    }
