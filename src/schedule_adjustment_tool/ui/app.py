from __future__ import annotations

import html
import hashlib
import logging
import time as time_module
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.domain.app_config import (
    load_app_settings,
)
from schedule_adjustment_tool.domain.auth import (
    ROLE_PARTICIPANT,
    ROLE_SCHEDULE_MANAGER,
    ROLE_SYSTEM_ADMIN,
    Principal,
    allowed_operation_roles,
    can_access_project,
    maintenance_mode_enabled,
)
from schedule_adjustment_tool.domain.evaluation_config import EVALUATION_SCORE_VERSION
from schedule_adjustment_tool.ui.calendar_views import (
    calendar_table_html,
)
from schedule_adjustment_tool.domain.models import (
    CANDIDATE_SEARCH_MODE_AUTO,
    Config,
    PARTICIPATION_MODE_TOTAL_ONCE,
    Participant,
    ROLE_DISPLAY_LABELS,
    WEEKDAY_LABELS,
    now_iso,
)
from schedule_adjustment_tool.ui.availability_grid_component import availability_grid
from schedule_adjustment_tool.storage.performance import log_storage_event
from schedule_adjustment_tool.ui.schedule_calendar_editor_component import (
    schedule_calendar_editor,
)
from schedule_adjustment_tool.ui.manager import (
    render_manager_project_selector,
    render_manager_shell,
)
from schedule_adjustment_tool.ui.manager.route_handlers import (
    ManagerScreenRenderers,
    build_manager_route_handlers,
)
from schedule_adjustment_tool.ui.manager.candidates import (
    CandidateScreenServices,
    render_candidate_adjustment_screen as render_candidate_adjustment_screen_view,
    render_candidate_list_screen as render_candidate_list_screen_view,
    render_candidate_publish_screen as render_candidate_publish_screen_view,
)
from schedule_adjustment_tool.ui.manager.participants import (
    ParticipantManagementServices,
    render_common_participant_addition_table as render_common_participant_addition_table_screen,
    render_participant_individual_conditions as render_participant_individual_conditions_screen,
    render_participant_membership as render_participant_membership_screen,
    render_participant_roster as render_participant_roster_screen,
)
from schedule_adjustment_tool.ui.manager.responses import (
    ResponseScreenServices,
    render_response_calendar as render_response_calendar_view,
    render_response_list as render_response_list_view,
    render_response_reminder as render_response_reminder_view,
    response_status_rows as response_status_rows_view,
)
from schedule_adjustment_tool.ui.manager.project_setup import (
    ProjectSetupServices,
    render_project_basic_settings as render_project_basic_settings_view,
    render_response_window_settings as render_response_window_settings_view,
)
from schedule_adjustment_tool.ui.manager.exports import (
    ExportScreenServices,
    project_candidates_export_bytes as project_candidates_export_bytes_view,
    project_confirmed_export_bytes as project_confirmed_export_bytes_view,
    project_input_status_export_bytes as project_input_status_export_bytes_view,
    render_on_demand_project_export as render_on_demand_project_export_view,
    render_prepared_download as render_prepared_download_view,
    render_project_participant_exports as render_project_participant_exports_view,
)
from schedule_adjustment_tool.ui.manager.current_schedule import (
    CurrentScheduleServices,
    render_current_schedule_operations as render_current_schedule_operations_view,
    render_schedule_revision_history as render_schedule_revision_history_view,
)
from schedule_adjustment_tool.ui.manager.candidate_calendar import (
    assignment_locks_from_calendar_sessions,
    calendar_required_role_counts,
    calendar_required_total_count,
    candidate_from_calendar_sessions,
    schedule_calendar_initial_sessions,
    schedule_from_calendar_sessions,
    schedule_from_editor,
)
from schedule_adjustment_tool.ui.manager.candidate_evaluation import (
    LEGACY_EVALUATION_SOURCE,
)
from schedule_adjustment_tool.ui.manager.candidate_operations import (
    CandidateOperationServices,
    render_candidate_calendar_actions as render_candidate_calendar_actions_view,
    show_candidate as show_candidate_view,
)
from schedule_adjustment_tool.ui.manager.candidate_generation import (
    CandidateGenerationServices,
    render_candidates as render_candidates_view,
)
from schedule_adjustment_tool.ui.manager.amendment_workspace import (
    AmendmentWorkspaceServices,
    render_amendment_requests_tab as render_amendment_requests_tab_view,
    render_schedule_amendments as render_schedule_amendments_view,
)
from schedule_adjustment_tool.ui.manager.lifecycle_dialogs import (
    backup_restore_confirmation_dialog,
    candidate_replacement_confirmation_dialog,
    common_participant_delete_confirmation_dialog,
    delete_project_confirmation_dialog,
    reset_confirmation_dialog,
)
from schedule_adjustment_tool.ui.manager.publication_dialogs import (
    confirmed_schedule_clear_confirmation_dialog,
    schedule_revision_restore_confirmation_dialog,
)
from schedule_adjustment_tool.ui.manager.project_change_dialogs import (
    ProjectChangeDialogServices,
    global_condition_change_confirmation_dialog as global_condition_change_confirmation_dialog_view,
    published_membership_change_confirmation_dialog as published_membership_change_confirmation_dialog_view,
    published_participant_change_confirmation_dialog as published_participant_change_confirmation_dialog_view,
    published_schedule_change_confirmation_dialog as published_schedule_change_confirmation_dialog_view,
    response_reopen_confirmation_dialog as response_reopen_confirmation_dialog_view,
)
from schedule_adjustment_tool.ui.manager.response_dialogs import (
    ResponseDialogServices,
    manager_participant_response_confirmation_dialog as manager_participant_response_confirmation_dialog_view,
    manager_response_restore_confirmation_dialog as manager_response_restore_confirmation_dialog_view,
)
from schedule_adjustment_tool.ui.manager.account_management import (
    AccountManagementServices,
    delete_project_participants_with_memberships as delete_project_participants_with_memberships_view,
    render_bulk_account_deletion,
    render_bulk_account_password_reset,
    render_individual_participant_account_generator,
    render_individual_participant_account_tools,
    render_participant_deletion_tools as render_participant_deletion_tools_view,
)
from schedule_adjustment_tool.ui.manager.project_creation import (
    manager_project_creation_dialog,
    render_system_project_creator,
)
from schedule_adjustment_tool.ui.manager.condition_settings import (
    ConditionSettingsServices,
    render_evaluation_preferences_settings as render_evaluation_preferences_settings_view,
    render_group_settings as render_group_settings_view,
    render_role_and_participation_settings as render_role_and_participation_settings_view,
)
from schedule_adjustment_tool.ui.manager.response_editor import (
    ResponseEditorServices,
    render_manager_participant_response_editor as render_manager_participant_response_editor_view,
)
from schedule_adjustment_tool.ui.manager.authentication import (
    render_authentication,
    render_project_access_gate,
)
from schedule_adjustment_tool.ui.manager.project_navigation import (
    ProjectNavigationServices,
    render_manager_project_access_settings as render_manager_project_access_settings_view,
    render_manager_project_sidebar_actions as render_manager_project_sidebar_actions_view,
)
from schedule_adjustment_tool.ui.manager.styles import render_main_styles
from schedule_adjustment_tool.ui.application_metadata import (
    APP_NAME,
    PAGE_ICON,
)
from schedule_adjustment_tool.ui.formatting import (
    format_datetime_with_weekday,
)
from schedule_adjustment_tool.ui.presentation import (
    CANDIDATE_SEARCH_MODE_LABELS,
    INPUT_STATUS_LABELS,
    STATUS_LABELS,
)
from schedule_adjustment_tool.ui.manager.common_participant_table import (
    common_participant_table_updates,
)
from schedule_adjustment_tool.ui.manager.app_cache import (
    clear_common_participants_cache,
    clear_prepared_exports,
    export_cache_token,
    load_common_participants_cached,
    load_project_list_cached,
    load_system_settings_cached,
    prepared_exports,
    record_audit_event_and_clear_cache,
    set_project_operation_feedback,
)
from schedule_adjustment_tool.ui.manager.project_cache import (
    append_unique_candidates,
    basic_project_settings_locked,
    cached_candidates,
    candidate_storage_version,
    close_expired_open_project,
    load_manager_project_overview_cached,
    load_project_confirmed_cached,
    load_project_participants_cached,
    project_config_draft,
    refresh_project_data_cache,
    refresh_project_list_cache,
    set_cached_candidates,
    set_cached_participants,
    update_cached_config,
    update_project_config_draft,
)
from schedule_adjustment_tool.ui.manager.system_management import (
    SystemManagementCallbacks,
    render_system_accounts_section as render_system_accounts_section_view,
    render_system_maintenance_mode_section as render_system_maintenance_mode_section_view,
    render_system_maintenance_section as render_system_maintenance_section_view,
    render_system_management as render_system_management_view,
    render_system_participants_section as render_system_participants_section_view,
    render_system_projects_section as render_system_projects_section_view,
    render_system_settings_section as render_system_settings_section_view,
    system_management_callbacks,
)
from schedule_adjustment_tool.ui.manager.session_state import (
    invalidate_manager_steps,
    mark_manager_step_started,
)
from schedule_adjustment_tool.ui.manager.view_models import (
    build_manager_project_summary_from_overview,
)
from schedule_adjustment_tool.ui.manager.version_update_notices import (
    begin_version_update_notice_render,
    render_legacy_candidate_evaluation_notice,
    render_support_role_update_notice,
)
from schedule_adjustment_tool.exports.spreadsheet_exports import (
    input_status_workbook,
)
from schedule_adjustment_tool.storage import (
    StorageConflictError,
    StorageError,
    acquire_job_lock,
    list_schedule_revisions,
    load_cross_project_blocked_slots,
    load_participants,
    release_job_lock,
    save_candidates,
    save_manager_response_override,
    save_participant_admin_fields_bulk,
    save_project_state_updates,
)


st.set_page_config(page_title=APP_NAME, page_icon=PAGE_ICON, layout="wide")
APP_SETTINGS = load_app_settings()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("schedule_adjustment_tool")
PREPARED_EXPORT_CACHE_LIMIT = 4
LAST_REFRESH_TIMES_KEY = "last_refresh_times"

def show_note(message: str) -> None:
    st.caption(message)


def render_availability_input_guide() -> None:
    st.caption(
        "通常日は各コマのチェックで参加可/不可を切り替えます。"
        "「Zoomなら可」をONにした日は、チェック済みコマがZoom可になり、"
        "Zoom/対面ラベルで参加方法を切り替えます。"
    )


def run_with_status(message: str, operation, *args, **kwargs):
    with st.spinner(message, show_time=True):
        return operation(*args, **kwargs)


def status_message(message: str):
    return st.spinner(message, show_time=True)


def scheduling_target_participants(
    participants: list[Participant],
) -> list[Participant]:
    return [
        participant
        for participant in participants
        if participant.active and participant.approved
    ]


def record_refresh_time(scope: str) -> None:
    refresh_times = st.session_state.setdefault(LAST_REFRESH_TIMES_KEY, {})
    refresh_times[scope] = datetime.now(
        ZoneInfo(APP_SETTINGS.timezone)
    ).strftime("%Y-%m-%d %H:%M:%S")


def last_refresh_time(scope: str) -> str:
    return str(
        st.session_state.get(LAST_REFRESH_TIMES_KEY, {}).get(scope, "")
    )


def scheduler_tools():
    from schedule_adjustment_tool.domain.scheduler import (
        candidate_fingerprint,
        candidate_sort_key,
        candidate_has_evaluation_config,
        diagnose_infeasibility,
        generate_amendment_candidates,
        generate_candidates,
        refresh_candidate_evaluation,
    )

    return {
        "candidate_fingerprint": candidate_fingerprint,
        "candidate_sort_key": candidate_sort_key,
        "candidate_has_evaluation_config": candidate_has_evaluation_config,
        "diagnose_infeasibility": diagnose_infeasibility,
        "generate_amendment_candidates": generate_amendment_candidates,
        "generate_candidates": generate_candidates,
        "refresh_candidate_evaluation": refresh_candidate_evaluation,
    }


def refresh_project_participants_cache(
    project_id: str,
) -> list[Participant]:
    """Reload participants and keep the project cache coherent."""

    participants = load_participants(project_id)
    set_cached_participants(project_id, participants)
    return deepcopy(participants)


def _export_screen_services() -> ExportScreenServices:
    """Bind export controls to the app-level cache and audit lifecycle."""

    return ExportScreenServices(
        prepared_exports=prepared_exports,
        prepared_export_cache_limit=PREPARED_EXPORT_CACHE_LIMIT,
        run_with_status=run_with_status,
        record_audit_event=record_audit_event_and_clear_cache,
        scheduling_target_participants=scheduling_target_participants,
    )


def render_prepared_download(
    container,
    *,
    project_id: str,
    kind: str,
    cache_token: str,
    prepare_label: str,
    download_label: str,
    status_label: str,
    build,
    build_args: tuple = (),
    file_name: str,
    audit_action: str,
) -> None:
    """Compatibility entrypoint for deferred manager downloads."""

    render_prepared_download_view(
        container,
        project_id=project_id,
        kind=kind,
        cache_token=cache_token,
        prepare_label=prepare_label,
        download_label=download_label,
        status_label=status_label,
        build=build,
        build_args=build_args,
        file_name=file_name,
        audit_action=audit_action,
        services=_export_screen_services(),
    )


def _project_input_status_export_bytes(project_id: str) -> bytes:
    return project_input_status_export_bytes_view(
        project_id,
        services=_export_screen_services(),
    )


def _project_candidates_export_bytes(project_id: str) -> bytes:
    return project_candidates_export_bytes_view(project_id)


def _project_confirmed_export_bytes(project_id: str) -> bytes:
    return project_confirmed_export_bytes_view(project_id)


def render_on_demand_project_export(
    *,
    project_id: str,
    kind: str,
    title: str,
    description: str,
    prepare_label: str,
    download_label: str,
    status_label: str,
    file_name: str,
    audit_action: str,
    build: Callable[[str], bytes],
) -> None:
    """Compatibility entrypoint for a project-specific spreadsheet export."""

    render_on_demand_project_export_view(
        project_id=project_id,
        kind=kind,
        title=title,
        description=description,
        prepare_label=prepare_label,
        download_label=download_label,
        status_label=status_label,
        file_name=file_name,
        audit_action=audit_action,
        build=build,
        services=_export_screen_services(),
    )


def render_project_participant_exports(
    project_id: str,
    config: Config,
    participants: list[Participant],
) -> None:
    """Render deferred project exports without loading them on page open."""

    render_project_participant_exports_view(
        project_id,
        config,
        participants,
        services=_export_screen_services(),
    )


def render_participant_deletion_tools(
    project_id: str,
    participants: list[Participant],
) -> None:
    """Compatibility entrypoint for roster deletion controls."""

    render_participant_deletion_tools_view(
        project_id,
        participants,
        services=AccountManagementServices(
            clear_candidate_state=clear_candidate_state,
        ),
    )


def delete_project_participants_with_memberships(
    project_id: str,
    participant_ids: list[str],
) -> None:
    """Compatibility entrypoint for project roster deletion."""

    delete_project_participants_with_memberships_view(
        project_id,
        participant_ids,
        services=AccountManagementServices(
            clear_candidate_state=clear_candidate_state,
        ),
    )


def candidate_affecting_config_keys() -> set[str]:
    return {
        "participation_requirement_mode",
        "required_total_count",
        "university_role_size",
        "high_school_role_size",
        "required_university_count",
        "required_high_school_count",
        "total_extra_limit",
    }


def published_schedule_affecting_config_keys() -> set[str]:
    return {
        "start_date",
        "end_date",
        "enabled_weekdays",
        "enabled_periods",
        "excluded_dates",
        "group_count",
        "group_field_assignments",
        "participation_requirement_mode",
        "required_total_count",
        "university_role_size",
        "high_school_role_size",
        "required_university_count",
        "required_high_school_count",
        "total_extra_limit",
        "max_groups_per_slot",
        "max_sessions_per_person_per_day",
        "avoid_consecutive_periods",
        "support_participation_limit",
    }


def changed_config_updates(config: Config, updates: dict) -> dict:
    current = config.to_dict()
    return {
        key: value
        for key, value in updates.items()
        if current.get(key) != value
    }


def candidate_deletion_count_for_config_updates(
    project_id: str,
    config_payload: dict[str, object],
    updates: dict[str, object],
) -> int:
    config = Config.from_dict(config_payload)
    changed = changed_config_updates(config, updates)
    if not candidate_affecting_config_keys() & set(changed):
        return 0
    return len(cached_candidates(project_id))


def update_config_and_clear_candidates_if_needed(
    project_id: str,
    config: Config,
    updates: dict,
) -> None:
    changed = changed_config_updates(config, updates)
    if not changed:
        update_cached_config(project_id, updates)
        update_project_config_draft(project_id, updates)
        return
    clear_candidates = bool(
        candidate_affecting_config_keys() & set(changed)
        and cached_candidates(project_id)
    )
    versions = save_project_state_updates(
        project_id,
        config_updates=changed,
        clear_candidates=clear_candidates,
        expected_config_version=getattr(config, "_storage_version", None),
        expected_candidate_version=(
            candidate_storage_version(project_id)
            if clear_candidates
            else None
        ),
    )
    storage_version = int(versions["config_version"] or 0)
    update_cached_config(
        project_id,
        changed,
        storage_version=storage_version,
    )
    update_project_config_draft(
        project_id,
        {**config.to_dict(), **changed},
        source_version=storage_version,
    )
    if clear_candidates:
        set_cached_candidates(
            project_id,
            [],
            version=int(versions["candidate_version"] or 0),
        )
        st.session_state.pop("candidate_reasons", None)
        clear_prepared_exports(project_id)
        invalidate_manager_steps(project_id, {"candidates", "publish"})


def section_heading(
    title: str,
    help_text: str,
    *,
    level: int = 3,
) -> None:
    safe_level = min(6, max(3, level))
    st.markdown(
        f"<div class='section-heading'><h{safe_level}>{html.escape(title)}</h{safe_level}></div>",
        unsafe_allow_html=True,
    )
    st.caption(help_text)


def render_calendar_table(
    frame: pd.DataFrame,
    *,
    config: Config,
    role_display_mode: str = ROLE_DISPLAY_LABELS,
    allow_cell_html: bool = False,
) -> None:
    st.markdown(
        calendar_table_html(
            frame,
            config=config,
            allow_cell_html=allow_cell_html,
        ),
        unsafe_allow_html=True,
    )


def reset_individual_conditions_to_defaults(
    project_id: str,
    participants: list[Participant],
    config: Config,
) -> None:
    updated_participants: list[Participant] = []
    for source in participants:
        participant = participant_with_default_conditions(source, config)
        participant.updated_at = now_iso()
        updated_participants.append(participant)
    if updated_participants:
        save_participant_admin_fields_bulk(project_id, updated_participants)
        set_cached_participants(project_id, updated_participants)
        clear_common_participants_cache()
    clear_candidate_state(project_id)


def participant_with_default_conditions(
    source: Participant,
    config: Config,
) -> Participant:
    participant = Participant.from_dict(source.to_dict())
    if config.participation_requirement_mode == PARTICIPATION_MODE_TOTAL_ONCE:
        participant.practice_role_unspecified = True
        participant.practice_participation_count = int(config.required_total_count)
        participant.required_university_count = 0
        participant.required_high_school_count = 0
    else:
        participant.practice_role_unspecified = bool(source.is_support)
        participant.practice_participation_count = None
        participant.required_university_count = int(
            config.required_university_count
        )
        participant.required_high_school_count = int(
            config.required_high_school_count
        )
    participant.total_extra_limit = int(config.total_extra_limit)
    return participant


def save_global_conditions_and_reset_individual_conditions(
    project_id: str,
    config: Config,
    participants: list[Participant],
    updates: dict,
) -> None:
    updated_config = Config.from_dict({**config.to_dict(), **updates})
    updated_participants: list[Participant] = []
    for source in participants:
        participant = participant_with_default_conditions(source, updated_config)
        participant.updated_at = now_iso()
        updated_participants.append(participant)
    changed = changed_config_updates(config, updates)
    clear_candidates = bool(
        candidate_affecting_config_keys() & set(changed)
        and cached_candidates(project_id)
    )
    versions = save_project_state_updates(
        project_id,
        config_updates=changed,
        participants=updated_participants,
        clear_candidates=clear_candidates,
        expected_config_version=getattr(config, "_storage_version", None),
        expected_candidate_version=(
            candidate_storage_version(project_id)
            if clear_candidates
            else None
        ),
    )
    config_version = int(versions["config_version"] or 0)
    update_cached_config(project_id, updates, storage_version=config_version)
    update_project_config_draft(
        project_id,
        {**config.to_dict(), **updates},
        source_version=config_version,
    )
    set_cached_participants(project_id, updated_participants)
    clear_common_participants_cache()
    if clear_candidates:
        set_cached_candidates(
            project_id,
            [],
            version=int(versions["candidate_version"] or 0),
        )
        clear_prepared_exports(project_id)
    invalidate_manager_steps(project_id, {"candidates", "publish"})


def save_participant_updates_and_clear_candidates(
    project_id: str,
    participants: list[Participant],
) -> None:
    clear_candidates = bool(cached_candidates(project_id))
    versions = save_project_state_updates(
        project_id,
        participants=participants,
        clear_candidates=clear_candidates,
        expected_candidate_version=(
            candidate_storage_version(project_id)
            if clear_candidates
            else None
        ),
    )
    refresh_project_participants_cache(project_id)
    clear_common_participants_cache()
    if clear_candidates:
        set_cached_candidates(
            project_id,
            [],
            version=int(versions["candidate_version"] or 0),
        )
        clear_prepared_exports(project_id)
        invalidate_manager_steps(project_id, {"candidates", "publish"})


def _project_change_dialog_services() -> ProjectChangeDialogServices:
    """Bind change confirmations to existing storage-adjacent operations."""

    return ProjectChangeDialogServices(
        apply_config_updates=update_config_and_clear_candidates_if_needed,
        reset_individual_conditions=reset_individual_conditions_to_defaults,
        save_global_conditions=(
            save_global_conditions_and_reset_individual_conditions
        ),
        apply_participant_updates=(
            save_participant_updates_and_clear_candidates
        ),
        save_project_settings=save_focused_project_settings,
        refresh_project_participants=refresh_project_participants_cache,
        clear_candidate_state=clear_candidate_state,
        mark_step_started=mark_manager_step_started,
    )


def global_condition_change_confirmation_dialog(
    project_id: str,
    config_payload: dict[str, object],
    participants_payload: list[dict[str, object]],
    updates: dict[str, object],
    confirmed: dict | None = None,
) -> None:
    """Compatibility wrapper for global-condition reset confirmation."""

    global_condition_change_confirmation_dialog_view(
        project_id,
        config_payload,
        participants_payload,
        updates,
        confirmed,
        candidate_count=candidate_deletion_count_for_config_updates(
            project_id,
            config_payload,
            updates,
        ),
        operations=_project_change_dialog_services(),
    )




def find_participant(
    participants: list[Participant], participant_id: str
) -> Participant | None:
    return next(
        (participant for participant in participants if participant.id == participant_id),
        None,
    )


def clear_candidate_state(project_id: str) -> None:
    invalidate_manager_steps(project_id, {"candidates", "publish"})
    if cached_candidates(project_id):
        version = run_with_status(
            "保存済み候補を整理しています...",
            save_candidates,
            project_id,
            [],
            expected_version=candidate_storage_version(project_id),
        )
        set_cached_candidates(project_id, [], version=version)
    st.session_state.pop("candidate_reasons", None)
    clear_prepared_exports(project_id)


def published_schedule_change_confirmation_dialog(
    project_id: str,
    config_payload: dict[str, object],
    participants_payload: list[dict[str, object]],
    updates: dict[str, object],
    success_message: str,
    workflow_step_id: str | None,
    *,
    published_schedule_exists: bool = True,
    feedback_operation_key: str = "settings",
    feedback_session_key: str | None = None,
    cancel_reset_keys: tuple[str, ...] = (),
) -> None:
    """Compatibility wrapper for published-setting confirmation."""

    published_schedule_change_confirmation_dialog_view(
        project_id,
        config_payload,
        participants_payload,
        updates,
        success_message,
        workflow_step_id,
        candidate_count=candidate_deletion_count_for_config_updates(
            project_id,
            config_payload,
            updates,
        ),
        published_schedule_exists=published_schedule_exists,
        feedback_operation_key=feedback_operation_key,
        feedback_session_key=feedback_session_key,
        cancel_reset_keys=cancel_reset_keys,
        operations=_project_change_dialog_services(),
    )


def response_reopen_confirmation_dialog(
    project_id: str,
    config_payload: dict[str, object],
    participants_payload: list[dict[str, object]],
    updates: dict[str, object],
    confirmed: dict | None,
) -> None:
    """Compatibility wrapper for response-reopen confirmation."""

    response_reopen_confirmation_dialog_view(
        project_id,
        config_payload,
        participants_payload,
        updates,
        confirmed,
        operations=_project_change_dialog_services(),
    )


def published_participant_change_confirmation_dialog(
    project_id: str,
    updated_participants_payload: list[dict[str, object]],
    *,
    change_label: str,
    workflow_step_id: str | None = None,
    published_schedule_exists: bool = True,
    feedback_operation_key: str = "individual_conditions",
) -> None:
    """Compatibility wrapper for published participant changes."""

    published_participant_change_confirmation_dialog_view(
        project_id,
        updated_participants_payload,
        change_label=change_label,
        workflow_step_id=workflow_step_id,
        candidate_count=len(cached_candidates(project_id)),
        published_schedule_exists=published_schedule_exists,
        feedback_operation_key=feedback_operation_key,
        operations=_project_change_dialog_services(),
    )


def published_membership_change_confirmation_dialog(
    project_id: str,
    updated_participants_payload: list[dict[str, object]],
    *,
    published_schedule_exists: bool = True,
) -> None:
    """Compatibility wrapper for published membership changes."""

    published_membership_change_confirmation_dialog_view(
        project_id,
        updated_participants_payload,
        candidate_count=len(cached_candidates(project_id)),
        published_schedule_exists=published_schedule_exists,
        feedback_operation_key="participant_membership",
        operations=_project_change_dialog_services(),
    )


def save_focused_project_settings(
    project_id: str,
    config: Config,
    participants: list[Participant],
    updates: dict[str, object],
    *,
    success_message: str,
    workflow_step_id: str | None = None,
    confirmed: dict | None = None,
    published_conflict: bool = False,
    feedback_operation_key: str = "settings",
) -> None:
    updated = Config.from_dict({**config.to_dict(), **updates})
    changed = changed_config_updates(config, updates)
    errors = updated.validate()
    highest_group = max(
        (
            int(participant.group_number)
            for participant in participants
            if participant.active and not participant.is_support
        ),
        default=1,
    )
    if updated.group_count < highest_group:
        errors.append(
            f"班の数を{highest_group}未満にできません。"
            "「参加者設定」で参加者の班を先に変更してください。"
        )
    if errors:
        for error in errors:
            st.error(error)
        return

    published_change_keys = (
        published_schedule_affecting_config_keys()
        | {
            "status",
            "response_deadline",
            "allow_edits_after_deadline",
        }
    )
    if (
        changed
        and (
            (
                published_conflict
                and confirmed
                and published_change_keys & set(changed)
            )
            or (
                candidate_affecting_config_keys() & set(changed)
                and cached_candidates(project_id)
            )
        )
    ):
        published_schedule_change_confirmation_dialog(
            project_id,
            config.to_dict(),
            [participant.to_dict() for participant in participants],
            updates,
            success_message,
            workflow_step_id,
            published_schedule_exists=bool(confirmed),
            feedback_operation_key=feedback_operation_key,
        )
        return

    deleted_candidate_count = (
        len(cached_candidates(project_id))
        if candidate_affecting_config_keys() & set(changed)
        else 0
    )
    with status_message("設定を保存しています..."):
        update_config_and_clear_candidates_if_needed(
            project_id,
            config,
            updates,
        )
    if deleted_candidate_count:
        set_project_operation_feedback(
            project_id,
            f"{success_message} 保存済み候補{deleted_candidate_count}件を削除しました。",
            operation_key=feedback_operation_key,
        )
    elif changed:
        set_project_operation_feedback(
            project_id,
            success_message,
            operation_key=feedback_operation_key,
        )
    else:
        set_project_operation_feedback(
            project_id,
            "保存する変更はありません。",
            kind="info",
            operation_key=feedback_operation_key,
        )
    if workflow_step_id:
        mark_manager_step_started(project_id, workflow_step_id)
    st.rerun()


def _project_setup_services() -> ProjectSetupServices:
    return ProjectSetupServices(
        max_text_length=APP_SETTINGS.max_text_length,
        max_description_length=APP_SETTINGS.max_description_length,
        status_labels=STATUS_LABELS,
        weekday_labels=WEEKDAY_LABELS,
        basic_settings_locked=basic_project_settings_locked,
        project_config_draft=project_config_draft,
        save_project_settings=save_focused_project_settings,
        confirm_response_reopen=response_reopen_confirmation_dialog,
    )


def render_project_basic_settings(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None = None,
) -> None:
    """Render project name, description, and performance-date settings."""

    render_project_basic_settings_view(
        project_id,
        config,
        participants,
        confirmed,
        services=_project_setup_services(),
    )


def render_response_window_settings(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None = None,
) -> None:
    """Render response reception dates, status, and deadline settings."""

    render_response_window_settings_view(
        project_id,
        config,
        participants,
        confirmed,
        services=_project_setup_services(),
    )
def _condition_settings_services() -> ConditionSettingsServices:
    """Bind condition screens to existing saves, dialogs, and workflow state."""

    return ConditionSettingsServices(
        basic_project_settings_locked=basic_project_settings_locked,
        project_config_draft=project_config_draft,
        update_project_config_draft=update_project_config_draft,
        save_project_settings=save_focused_project_settings,
        changed_config_updates=changed_config_updates,
        update_config_and_clear_candidates=update_config_and_clear_candidates_if_needed,
        candidate_affecting_config_keys=candidate_affecting_config_keys,
        confirm_global_condition_change=(
            global_condition_change_confirmation_dialog
        ),
        confirm_published_schedule_change=(
            published_schedule_change_confirmation_dialog
        ),
        mark_step_started=mark_manager_step_started,
        show_note=show_note,
        section_heading=section_heading,
    )


def render_group_settings(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None = None,
) -> None:
    """Compatibility entrypoint for group composition settings."""

    render_group_settings_view(
        project_id,
        config,
        participants,
        confirmed,
        services=_condition_settings_services(),
    )


def render_role_and_participation_settings(
    project_id: str,
    config: Config,
    participants: list[Participant] | None = None,
    confirmed: dict | None = None,
) -> None:
    """Compatibility entrypoint for candidate role constraints."""

    render_role_and_participation_settings_view(
        project_id,
        config,
        participants,
        confirmed,
        services=_condition_settings_services(),
    )


def render_evaluation_preferences_settings(
    project_id: str,
    config: Config,
    participants: list[Participant] | None = None,
    confirmed: dict | None = None,
) -> None:
    """Compatibility entrypoint for evaluation preference settings."""

    render_evaluation_preferences_settings_view(
        project_id,
        config,
        participants,
        confirmed,
        services=_condition_settings_services(),
    )

def participant_with_updated_response(
    participant: Participant,
    *,
    availability: set[str],
    zoom_availability: set[str],
    input_status: str,
) -> Participant:
    if input_status not in {"draft", "submitted"}:
        raise ValueError("入力状態が不正です。")
    updated = Participant.from_dict(participant.to_dict())
    updated.availability = sorted(availability)
    updated.zoom_availability = sorted(zoom_availability - availability)
    updated.input_status = input_status
    updated.updated_at = now_iso()
    if input_status == "submitted":
        updated.submitted_at = updated.updated_at
    return updated


def save_manager_participant_response(
    project_id: str,
    participant: Participant,
    *,
    selected_slots: set[str],
    selected_zoom_slots: set[str],
    input_status: str,
) -> Participant:
    updated = participant_with_updated_response(
        participant,
        availability=selected_slots,
        zoom_availability=selected_zoom_slots,
        input_status=input_status,
    )
    save_manager_response_override(
        project_id,
        updated,
        expected_version=participant.storage_version,
    )
    refreshed = refresh_project_participants_cache(project_id)
    clear_candidate_state(project_id)
    return (
        find_participant(refreshed, participant.id)
        or updated
    )


def _response_dialog_services() -> ResponseDialogServices:
    """Bind response confirmations to existing response and cache operations."""

    return ResponseDialogServices(
        input_status_labels=INPUT_STATUS_LABELS,
        save_manager_response=save_manager_participant_response,
        refresh_project_participants=refresh_project_participants_cache,
        clear_candidate_state=clear_candidate_state,
    )


def manager_participant_response_confirmation_dialog(
    project_id: str,
    participant_payload: dict[str, object],
    selected_slots: set[str],
    selected_zoom_slots: set[str],
    input_status: str,
) -> None:
    """Compatibility wrapper for manager-response confirmation."""

    manager_participant_response_confirmation_dialog_view(
        project_id,
        participant_payload,
        selected_slots,
        selected_zoom_slots,
        input_status,
        operations=_response_dialog_services(),
    )


def manager_response_restore_confirmation_dialog(
    project_id: str,
    participant_payload: dict[str, object],
) -> None:
    """Compatibility wrapper for restoring a participant's own response."""

    manager_response_restore_confirmation_dialog_view(
        project_id,
        participant_payload,
        operations=_response_dialog_services(),
    )


def _response_editor_services() -> ResponseEditorServices:
    """Bind the manager response editor to compatibility dialogs."""

    return ResponseEditorServices(
        input_status_labels=INPUT_STATUS_LABELS,
        render_availability_input_guide=render_availability_input_guide,
        availability_grid=availability_grid,
        confirm_manager_response=(
            manager_participant_response_confirmation_dialog
        ),
        confirm_response_restore=manager_response_restore_confirmation_dialog,
    )


def render_manager_participant_response_editor(
    project_id: str,
    config: Config,
    participants: list[Participant],
    *,
    embedded: bool = False,
) -> None:
    """Compatibility entrypoint for manager response overrides."""

    render_manager_participant_response_editor_view(
        project_id,
        config,
        participants,
        embedded=embedded,
        services=_response_editor_services(),
    )

def _participant_management_services() -> ParticipantManagementServices:
    """Bind participant UI renderers to app cache and workflow coordination."""

    return ParticipantManagementServices(
        max_text_length=APP_SETTINGS.max_text_length,
        max_description_length=APP_SETTINGS.max_description_length,
        load_common_participants=load_common_participants_cached,
        load_system_settings=load_system_settings_cached,
        set_cached_participants=set_cached_participants,
        clear_common_participants_cache=clear_common_participants_cache,
        clear_candidate_state=clear_candidate_state,
        mark_step_started=mark_manager_step_started,
        status_message=status_message,
        render_participant_deletion_tools=render_participant_deletion_tools,
        confirm_membership_change=published_membership_change_confirmation_dialog,
        confirm_individual_condition_change=(
            published_participant_change_confirmation_dialog
        ),
        save_participant_updates=save_participant_updates_and_clear_candidates,
        candidate_count=lambda project_id: len(cached_candidates(project_id)),
    )


def render_common_participant_addition_table(
    project_id: str,
    participants: list[Participant],
) -> None:
    """Compatibility entrypoint for the participant roster screen."""

    render_common_participant_addition_table_screen(
        project_id,
        participants,
        services=_participant_management_services(),
    )


def render_participant_roster(
    project_id: str,
    config: Config,
    participants: list[Participant],
) -> None:
    """Render participant creation and common-roster addition."""

    render_participant_roster_screen(
        project_id,
        config,
        participants,
        services=_participant_management_services(),
    )


def render_participant_membership(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None = None,
) -> None:
    """Render per-project participant membership fields."""

    render_participant_membership_screen(
        project_id,
        config,
        participants,
        confirmed,
        services=_participant_management_services(),
    )


def render_participant_individual_conditions(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None = None,
) -> None:
    """Render per-participant scheduling conditions."""

    render_participant_individual_conditions_screen(
        project_id,
        config,
        participants,
        confirmed,
        services=_participant_management_services(),
    )
def _response_screen_services() -> ResponseScreenServices:
    return ResponseScreenServices(
        input_status_labels=INPUT_STATUS_LABELS,
        format_datetime=format_datetime_with_weekday,
        render_calendar_table=render_calendar_table,
    )


def response_status_rows(
    participants: list[Participant],
) -> list[dict[str, object]]:
    """Return display rows for response status and availability review."""

    return response_status_rows_view(
        participants,
        services=_response_screen_services(),
    )


def render_response_list(
    config: Config,
    participants: list[Participant],
    *,
    status_only: bool,
) -> None:
    render_response_list_view(
        config,
        participants,
        status_only=status_only,
        services=_response_screen_services(),
    )


def render_response_calendar(
    config: Config,
    participants: list[Participant],
) -> None:
    render_response_calendar_view(
        config,
        participants,
        services=_response_screen_services(),
    )


def render_response_reminder(participants: list[Participant]) -> None:
    render_response_reminder_view(participants)
def render_status(
    config: Config,
    participants: list[Participant],
    *,
    section: str = "all",
    show_header: bool = True,
) -> None:
    section_titles = {
        "all": "入力状況・代理入力",
        "status": "提出状況",
        "content": "回答内容",
        "proxy": "代理入力",
    }
    if section not in section_titles:
        raise ValueError(f"Unknown response section: {section}")
    if show_header:
        st.header(section_titles[section])
    if not participants:
        st.info("参加者はまだ登録されていません。")
        return
    all_participants = participants
    if section == "all":
        st.caption(
            "提出状況の確認、回答内容の確認、代理入力を行えます。"
        )
        if st.toggle(
            "参加者の回答を代理入力・修正する",
            value=False,
            key=f"show_manager_response_editor_{config.project_id}",
            help=(
                "参加者を選び、代理入力を保存できます。"
                "本人の入力は別に残り、必要な時に戻せます。"
            ),
        ):
            with st.container(border=True):
                render_manager_participant_response_editor(
                    config.project_id,
                    config,
                    all_participants,
                    embedded=True,
                )
            st.divider()
    elif section == "proxy":
        render_manager_participant_response_editor(
            config.project_id,
            config,
            all_participants,
            embedded=True,
        )
        return

    target_participants = scheduling_target_participants(all_participants)
    if not target_participants:
        st.info("この企画の日程調整対象者はまだ選択されていません。")
        return
    if section == "status":
        render_response_list(
            config,
            target_participants,
            status_only=True,
        )
        render_response_reminder(target_participants)
        return

    view_column, export_column = st.columns([2.4, 1], gap="large")
    with view_column:
        selected_view = st.segmented_control(
            "表示方法",
            ["一覧", "カレンダー"],
            default="一覧",
            key=f"availability_view_{config.project_id}",
        )
    with export_column:
        render_prepared_download(
            export_column,
            project_id=config.project_id,
            kind="response_content",
            cache_token=export_cache_token(
                {
                    "config": config.to_dict(),
                    "participants": [
                        item.to_dict() for item in target_participants
                    ],
                }
            ),
            prepare_label="回答内容をExcel準備",
            download_label="回答内容をExcel出力",
            status_label="回答内容のExcelを準備しています...",
            build=input_status_workbook,
            build_args=(config, target_participants),
            file_name=f"{config.title}_回答内容.xlsx",
            audit_action="input_status_workbook.exported",
        )
    if selected_view == "一覧":
        render_response_list(
            config,
            target_participants,
            status_only=False,
        )
    else:
        render_response_calendar(config, target_participants)
    if section == "all":
        render_response_reminder(target_participants)


def append_project_candidates(
    project_id: str,
    existing_candidates: list[dict],
    new_candidates: list[dict],
) -> tuple[list[dict], list[dict], int, int]:
    tools = scheduler_tools()
    result = append_unique_candidates(
        existing_candidates,
        new_candidates,
        fingerprint=tools["candidate_fingerprint"],
        max_candidates=APP_SETTINGS.max_stored_candidates,
    )
    merged_candidates, added_candidates, _, _ = result
    if added_candidates:
        with status_message("候補を保存しています..."):
            version = save_candidates(
                project_id,
                merged_candidates,
                expected_version=candidate_storage_version(project_id),
            )
            set_cached_candidates(project_id, merged_candidates, version=version)
            mark_manager_step_started(project_id, "candidates")
    return result


def calendar_candidate_token(candidate: dict) -> str:
    fingerprint = scheduler_tools()["candidate_fingerprint"](candidate)
    return hashlib.sha256(repr(fingerprint).encode("utf-8")).hexdigest()[:12]


def generate_candidates_with_assignment_locks(
    project_id: str,
    config: Config,
    participants: list[Participant],
    existing_candidates: list[dict],
    assignment_locks: list[dict],
    *,
    candidate_limit: int,
    timeout_seconds: int,
    random_seed: int,
) -> tuple[list[dict], list[str], str]:
    search_owner = st.session_state.setdefault(
        "search_owner_id",
        uuid4().hex,
    )
    if not acquire_job_lock(
        project_id,
        search_owner,
        timeout_seconds + 30,
    ):
        return [], [], "この企画では別の探索が実行中です。"
    try:
        target_ids = [
            participant.id
            for participant in scheduling_target_participants(participants)
        ]
        blocked_slots = load_cross_project_blocked_slots(
            project_id,
            target_ids,
        )
        candidates, reasons = scheduler_tools()["generate_candidates"](
            config,
            participants,
            timeout_seconds=timeout_seconds,
            random_seed=random_seed,
            candidate_limit=candidate_limit,
            excluded_candidates=existing_candidates,
            blocked_slots_by_participant=blocked_slots,
            assignment_locks=assignment_locks,
        )
    finally:
        release_job_lock(project_id, search_owner)
    return candidates, reasons, ""


def _candidate_operation_services() -> CandidateOperationServices:
    """Bind candidate interaction UI to existing application operations."""

    return CandidateOperationServices(
        max_stored_candidates=APP_SETTINGS.max_stored_candidates,
        schedule_calendar_editor=schedule_calendar_editor,
        render_prepared_download=render_prepared_download,
        append_project_candidates=append_project_candidates,
        generate_candidates_with_assignment_locks=(
            generate_candidates_with_assignment_locks
        ),
    )


def show_candidate(
    project_id: str,
    config: Config,
    candidate: dict,
    index: int,
    confirmable: bool,
    allow_download: bool = True,
    calendar_first: bool = False,
    expected_revision_id: str | None = None,
    participants: list[Participant] | None = None,
    expanded: bool | None = None,
    candidate_version: int | None = None,
    *,
    evaluation_context: dict[str, object] | None = None,
    candidate_for_confirmation: dict | None = None,
    policy_issues: tuple[str, ...] = (),
) -> None:
    """Compatibility wrapper for the candidate detail renderer."""

    show_candidate_view(
        project_id,
        config,
        candidate,
        index,
        confirmable,
        allow_download=allow_download,
        calendar_first=calendar_first,
        expected_revision_id=expected_revision_id,
        participants=participants,
        expanded=expanded,
        candidate_version=candidate_version,
        evaluation_context=evaluation_context,
        candidate_for_confirmation=candidate_for_confirmation,
        policy_issues=policy_issues,
        operations=_candidate_operation_services(),
    )


def render_candidate_calendar_actions(
    project_id: str,
    config: Config,
    participants: list[Participant],
    existing_candidates: list[dict],
    *,
    base_candidate: dict | None,
    initial_sessions: list[dict],
    editor_key: str,
    direct_origin: dict[str, object],
    optimization_origin: dict[str, object],
    direct_button_label: str,
    optimization_button_label: str,
    search_count: int,
    search_timeout: int,
    search_seed: int,
    ready: bool,
    allow_optimization: bool = True,
    show_date_lock_controls: bool = True,
) -> None:
    """Compatibility wrapper for calendar-based candidate operations."""

    render_candidate_calendar_actions_view(
        project_id,
        config,
        participants,
        existing_candidates,
        base_candidate=base_candidate,
        initial_sessions=initial_sessions,
        editor_key=editor_key,
        direct_origin=direct_origin,
        optimization_origin=optimization_origin,
        direct_button_label=direct_button_label,
        optimization_button_label=optimization_button_label,
        search_count=search_count,
        search_timeout=search_timeout,
        search_seed=search_seed,
        ready=ready,
        allow_optimization=allow_optimization,
        show_date_lock_controls=show_date_lock_controls,
        operations=_candidate_operation_services(),
    )


def render_candidate_search_settings(
    project_id: str,
    config: Config,
    *,
    expanded: bool = False,
) -> tuple[int, int, int, str]:
    with st.expander(
        "探索方法",
        expanded=expanded,
    ):
        st.caption(
            "探索モード、1回に作る候補数、探索時間、再現用シードを設定します。"
            "探索時間上限が長いほど、より良い候補を見つけやすくなります。"
            "候補の確保と改善・別候補探索は、全体で1つの時間上限を共有します。"
        )
        with st.form(
            f"search_settings_{project_id}",
            enter_to_submit=False,
        ):
            search_columns = st.columns([1.55, 1, 1, 1, 1])
            search_mode_label = search_columns[0].selectbox(
                "探索モード",
                list(CANDIDATE_SEARCH_MODE_LABELS.values()),
                index=list(CANDIDATE_SEARCH_MODE_LABELS.values()).index(
                    CANDIDATE_SEARCH_MODE_LABELS.get(
                        config.candidate_search_mode,
                        CANDIDATE_SEARCH_MODE_LABELS[CANDIDATE_SEARCH_MODE_AUTO],
                    )
                ),
                help=(
                    "自動は必須条件を満たす候補を先に確保し、不成立を証明できた"
                    "場合だけ近似へ進みます。厳密は近似へ進みません。近似は"
                    "違反量、不要な追加参加、総合適合度の順に探索します。"
                ),
                key=f"search_mode_{project_id}",
            )
            search_count = int(
                search_columns[1].number_input(
                    "1回の探索数",
                    1,
                    APP_SETTINGS.max_candidates_per_search,
                    min(
                        config.max_candidates,
                        APP_SETTINGS.max_candidates_per_search,
                    ),
                    help="1回の探索で新しく探す候補数です。",
                    key=f"search_count_{project_id}",
                )
            )
            search_timeout = int(
                search_columns[2].number_input(
                    "探索時間上限（秒）",
                    1,
                    APP_SETTINGS.max_search_seconds,
                    min(
                        config.search_timeout_seconds,
                        APP_SETTINGS.max_search_seconds,
                    ),
                    help=(
                        "候補探索に使う最大秒数です。"
                        "長いほど探索範囲が広がります。"
                    ),
                    key=f"search_timeout_{project_id}",
                )
            )
            search_seed = int(
                search_columns[3].number_input(
                    "再現用シード",
                    0,
                    999999,
                    config.random_seed,
                    help="値を変えると別の候補を見つけやすくなります。",
                    key=f"search_seed_{project_id}",
                )
            )
            save_search_settings = search_columns[4].form_submit_button(
                "探索方法を保存"
            )
    search_mode = next(
        mode
        for mode, label in CANDIDATE_SEARCH_MODE_LABELS.items()
        if label == search_mode_label
    )
    if save_search_settings:
        updates = {
            "max_candidates": search_count,
            "search_timeout_seconds": search_timeout,
            "candidate_search_mode": search_mode,
            "random_seed": search_seed,
        }
        with status_message("探索方法を保存しています..."):
            changed = changed_config_updates(config, updates)
            update_config_and_clear_candidates_if_needed(
                project_id,
                config,
                updates,
            )
        if changed:
            st.success("探索方法を保存しました。")
        else:
            st.info("保存する変更はありません。")
        if changed:
            mark_manager_step_started(project_id, "conditions")
    return search_count, search_timeout, search_seed, search_mode


def refresh_saved_candidate_evaluations(
    project_id: str,
    config: Config,
    participants: list[Participant],
    candidates: list[dict],
) -> list[dict]:
    render_legacy_candidate_evaluation_notice(project_id, candidates)
    if not any(
        candidate.get("metrics", {}).get("evaluation_score_version")
        != EVALUATION_SCORE_VERSION
        or not _candidate_has_evaluation_snapshot(candidate)
        for candidate in candidates
    ):
        return candidates
    tools = scheduler_tools()
    with status_message("保存済み候補の評価を更新しています..."):
        for candidate in candidates:
            if not _candidate_has_evaluation_snapshot(candidate):
                candidate["evaluation_config_source"] = LEGACY_EVALUATION_SOURCE
        candidates = [
            tools["refresh_candidate_evaluation"](
                candidate,
                config,
                participants,
            )
            for candidate in candidates
        ]
        candidates.sort(key=tools["candidate_sort_key"])
        version = save_candidates(
            project_id,
            candidates,
            expected_version=candidate_storage_version(project_id),
        )
        set_cached_candidates(project_id, candidates, version=version)
    return candidates


def _candidate_has_evaluation_snapshot(candidate: dict) -> bool:
    snapshot = candidate.get("evaluation_config")
    return isinstance(snapshot, dict) and isinstance(
        snapshot.get("evaluation_settings"),
        dict,
    )


def _candidate_generation_services() -> CandidateGenerationServices:
    """Bind candidate generation UI to existing operations and dialogs."""

    return CandidateGenerationServices(
        max_stored_candidates=APP_SETTINGS.max_stored_candidates,
        scheduler_tools=scheduler_tools,
        render_search_settings=render_candidate_search_settings,
        scheduling_target_participants=scheduling_target_participants,
        confirm_replacement=candidate_replacement_confirmation_dialog,
        render_calendar_actions=render_candidate_calendar_actions,
        show_candidate=show_candidate,
        calendar_candidate_token=calendar_candidate_token,
    )


def render_candidates(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None = None,
    *,
    creation_only: bool = False,
) -> None:
    """Compatibility wrapper for candidate generation and comparison."""

    render_candidates_view(
        project_id,
        config,
        participants,
        confirmed,
        creation_only=creation_only,
        operations=_candidate_generation_services(),
    )


def _candidate_screen_services() -> CandidateScreenServices:
    """Bind focused candidate screens to the existing business operations."""

    return CandidateScreenServices(
        max_candidates_per_search=APP_SETTINGS.max_candidates_per_search,
        max_search_seconds=APP_SETTINGS.max_search_seconds,
        cached_candidates=cached_candidates,
        candidate_storage_version=candidate_storage_version,
        refresh_saved_candidate_evaluations=refresh_saved_candidate_evaluations,
        show_candidate=show_candidate,
        scheduling_target_participants=scheduling_target_participants,
        schedule_calendar_initial_sessions=schedule_calendar_initial_sessions,
        render_candidate_calendar_actions=render_candidate_calendar_actions,
        calendar_candidate_token=calendar_candidate_token,
    )


def render_candidate_list_screen(
    project_id: str,
    config: Config,
    participants: list[Participant],
) -> None:
    """Render saved-candidate comparison and detail."""

    render_candidate_list_screen_view(
        project_id,
        config,
        participants,
        services=_candidate_screen_services(),
    )


def render_candidate_adjustment_screen(
    project_id: str,
    config: Config,
    participants: list[Participant],
) -> None:
    """Render a non-destructive manual or partial-optimization adjustment."""

    render_candidate_adjustment_screen_view(
        project_id,
        config,
        participants,
        services=_candidate_screen_services(),
    )


def render_candidate_publish_screen(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None,
) -> None:
    """Render final candidate review and publication."""

    render_candidate_publish_screen_view(
        project_id,
        config,
        participants,
        confirmed,
        services=_candidate_screen_services(),
    )




def _current_schedule_services() -> CurrentScheduleServices:
    """Bind published-schedule screen actions without exposing ui.app to it."""

    return CurrentScheduleServices(
        list_schedule_revisions=list_schedule_revisions,
        show_candidate=show_candidate,
        format_datetime=format_datetime_with_weekday,
        render_prepared_download=render_prepared_download,
        export_cache_token=export_cache_token,
        confirm_revision_restore=schedule_revision_restore_confirmation_dialog,
        confirm_schedule_clear=confirmed_schedule_clear_confirmation_dialog,
    )


def render_schedule_revision_history(
    project_id: str,
    confirmed: dict | None,
) -> None:
    """Compatibility entrypoint for the published revision-history panel."""

    render_schedule_revision_history_view(
        project_id,
        confirmed,
        services=_current_schedule_services(),
    )




def _amendment_workspace_services() -> AmendmentWorkspaceServices:
    """Bind the amendment workspace to existing application operations."""

    return AmendmentWorkspaceServices(
        max_search_seconds=APP_SETTINGS.max_search_seconds,
        changed_config_updates=changed_config_updates,
        scheduler_tools=scheduler_tools,
        scheduling_target_participants=scheduling_target_participants,
        schedule_calendar_editor=schedule_calendar_editor,
        show_candidate=show_candidate,
    )


def render_amendment_requests_tab(
    project_id: str,
    config: Config,
    participants: list[Participant],
    amendment: dict | None,
    *,
    current_revision_id: str,
    public_version: int,
    workspace_version: int,
) -> None:
    """Compatibility wrapper for amendment request intake."""

    render_amendment_requests_tab_view(
        project_id,
        config,
        participants,
        amendment,
        current_revision_id=current_revision_id,
        public_version=public_version,
        workspace_version=workspace_version,
        operations=_amendment_workspace_services(),
    )


def render_schedule_amendments(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None,
) -> None:
    """Compatibility wrapper for the post-publication amendment workspace."""

    render_schedule_amendments_view(
        project_id,
        config,
        participants,
        confirmed,
        operations=_amendment_workspace_services(),
    )


def render_current_schedule_operations(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None,
) -> None:
    """Compatibility entrypoint for the current published-schedule screen."""

    render_current_schedule_operations_view(
        project_id,
        config,
        participants,
        confirmed,
        services=_current_schedule_services(),
    )


def _system_management_callbacks() -> SystemManagementCallbacks:
    """Bind system screens to the existing confirmation dialogs and tools."""

    return SystemManagementCallbacks(
        common_participant_table_updates=common_participant_table_updates,
        format_datetime=format_datetime_with_weekday,
        confirm_common_participant_delete=(
            common_participant_delete_confirmation_dialog
        ),
        render_bulk_password_reset=render_bulk_account_password_reset,
        render_bulk_account_delete=render_bulk_account_deletion,
        render_individual_account_generator=(
            render_individual_participant_account_generator
        ),
        render_project_creator=render_system_project_creator,
        confirm_project_reset=reset_confirmation_dialog,
        confirm_project_delete=delete_project_confirmation_dialog,
        confirm_backup_restore=backup_restore_confirmation_dialog,
    )


def _render_system_screen(view) -> None:
    with system_management_callbacks(_system_management_callbacks()):
        view()


def render_system_settings_section() -> None:
    _render_system_screen(render_system_settings_section_view)


def render_system_maintenance_mode_section() -> None:
    _render_system_screen(render_system_maintenance_mode_section_view)


def render_system_participants_section() -> None:
    _render_system_screen(render_system_participants_section_view)


def render_system_accounts_section() -> None:
    _render_system_screen(render_system_accounts_section_view)


def render_system_projects_section() -> None:
    _render_system_screen(render_system_projects_section_view)


def render_system_maintenance_section() -> None:
    _render_system_screen(render_system_maintenance_section_view)


def render_system_management() -> None:
    _render_system_screen(render_system_management_view)


def _project_navigation_services() -> ProjectNavigationServices:
    """Bind supporting project controls to cache and dialog operations."""

    return ProjectNavigationServices(
        status_message=status_message,
        update_config_and_clear_candidates=(
            update_config_and_clear_candidates_if_needed
        ),
        refresh_project_list=refresh_project_list_cache,
        refresh_project_data=refresh_project_data_cache,
        record_refresh_time=record_refresh_time,
        last_refresh_time=last_refresh_time,
        open_project_creation_dialog=manager_project_creation_dialog,
    )


def render_manager_project_access_settings(
    project_id: str,
    config: Config,
) -> None:
    """Compatibility entrypoint for manager project-password settings."""

    render_manager_project_access_settings_view(
        project_id,
        config,
        services=_project_navigation_services(),
    )


def render_manager_project_sidebar_actions(
    project_id: str,
    projects: list[dict],
    principal: Principal,
) -> None:
    """Compatibility entrypoint for manager sidebar project actions."""

    render_manager_project_sidebar_actions_view(
        project_id,
        projects,
        principal,
        services=_project_navigation_services(),
    )

def main() -> None:
    begin_version_update_notice_render()
    principal = render_authentication()
    rerender_started = st.session_state.pop(
        "schedule_confirm_rerender_started_at", None
    )
    if isinstance(rerender_started, (float, int)):
        rerender_elapsed = time_module.perf_counter() - float(rerender_started)
        if rerender_elapsed >= 0:
            log_storage_event(
                LOGGER,
                "schedule_confirm_rerender",
                rerender_seconds=round(rerender_elapsed, 6),
                total_seconds=round(rerender_elapsed, 6),
            )
    if not principal.is_system_admin and maintenance_mode_enabled(principal):
        st.warning("メンテナンス中")
        return
    operation_roles = allowed_operation_roles(principal)
    if not operation_roles:
        st.warning("利用可能な権限が割り当てられていません。")
        return
    if len(operation_roles) == 1:
        role = operation_roles[0]
        st.session_state["operation_role"] = role
    else:
        role = st.radio(
            "操作区分",
            operation_roles,
            horizontal=True,
            key="operation_role",
            help=(
                "認証ONでは、ログインユーザーに割り当てた権限だけを"
                "表示します。"
            ),
        )
    if role in {
        ROLE_SYSTEM_ADMIN,
        ROLE_SCHEDULE_MANAGER,
        ROLE_PARTICIPANT,
    }:
        render_main_styles()
    if role == ROLE_SYSTEM_ADMIN:
        if not principal.is_system_admin:
            st.error("システム管理者権限が必要です。")
            return
        render_system_management()
        return
    if role == ROLE_PARTICIPANT:
        from schedule_adjustment_tool.ui import (
            participant_workspace as participant_ui,
        )

        participant_ui.configure_participant_page(include_page_config=False)
        st.caption(
            "参加する企画の確認、参加可能日時の入力、"
            "確定日程の確認ができます。"
        )
        participant_ui.render_participant_workspace(principal)
        return

    if role != ROLE_SCHEDULE_MANAGER:
        st.error("この操作区分には対応していません。")
        return

    project_creation_notice = st.session_state.pop(
        "project_creation_notice", None
    )
    projects = load_project_list_cached()
    selectable_projects = [
        project
        for project in projects
        if not project.get("archived", False)
        and can_access_project(principal, str(project["id"]), role)
    ]
    if not selectable_projects:
        st.warning("この操作区分で利用できる企画がありません。")
        return
    project_ids = [str(project["id"]) for project in selectable_projects]
    active_project_id = str(
        st.session_state.get("active_project_id", "") or ""
    )
    if active_project_id not in project_ids:
        active_project_id = ""
    active_project_id = render_manager_project_selector(
        selectable_projects,
        active_project_id,
        force_active_project=bool(project_creation_notice),
    )
    if not active_project_id:
        st.session_state["active_project_id"] = ""
        st.info("まず企画を選択してください。")
        return
    if project_creation_notice:
        st.success(str(project_creation_notice))
    st.session_state["active_project_id"] = active_project_id
    project_overview = load_manager_project_overview_cached(active_project_id)
    refresh_scope = f"project:{active_project_id}"
    if not last_refresh_time(refresh_scope):
        record_refresh_time(refresh_scope)
    render_manager_project_sidebar_actions(
        active_project_id,
        selectable_projects,
        principal,
    )
    project_config = project_overview["config"]
    if not render_project_access_gate(
        active_project_id,
        project_config,
        principal,
    ):
        return
    render_support_role_update_notice(active_project_id)
    config = close_expired_open_project(active_project_id, project_config)
    project_overview["config"] = config
    summary = build_manager_project_summary_from_overview(
        active_project_id,
        config,
        project_overview,
    )
    render_manager_shell(
        active_project_id,
        config,
        [],
        [],
        None,
        summary=summary,
        route_handlers=build_manager_route_handlers(
            active_project_id,
            config,
            [],
            None,
            participants_loader=lambda: load_project_participants_cached(
                active_project_id
            ),
            confirmed_loader=lambda: load_project_confirmed_cached(
                active_project_id
            ),
            renderers=ManagerScreenRenderers(
                project_basic=render_project_basic_settings,
                response_window=render_response_window_settings,
                group_settings=render_group_settings,
                participant_roster=render_participant_roster,
                participant_membership=render_participant_membership,
                participant_accounts=render_individual_participant_account_tools,
                response_status=render_status,
                role_and_participation=render_role_and_participation_settings,
                participant_individual_conditions=(
                    render_participant_individual_conditions
                ),
                evaluation_preferences=render_evaluation_preferences_settings,
                candidate_search_settings=render_candidate_search_settings,
                candidates=render_candidates,
                candidate_list=render_candidate_list_screen,
                candidate_adjustment=render_candidate_adjustment_screen,
                candidate_publish=render_candidate_publish_screen,
                current_schedule=render_current_schedule_operations,
                schedule_amendments=render_schedule_amendments,
                project_exports=render_project_participant_exports,
                project_access=render_manager_project_access_settings,
            ),
        ),
    )


def run() -> None:
    try:
        main()
    except StorageConflictError as error:
        st.error(str(error))
        st.info("他の利用者の更新を反映するため、ページを再読み込みしてください。")
    except StorageError as error:
        st.error(f"データ保存エラー: {error}")
    except Exception:
        error_id = uuid4().hex[:12]
        LOGGER.exception("Unhandled application error id=%s", error_id)
        st.error(
            "予期しないエラーが発生しました。"
            f" 管理者へエラーID `{error_id}` を連絡してください。"
        )


if __name__ == "__main__":
    run()
