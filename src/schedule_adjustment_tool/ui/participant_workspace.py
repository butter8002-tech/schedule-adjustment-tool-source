from __future__ import annotations

import html
import logging
import time as time_module
from copy import copy, deepcopy
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import uuid4

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.domain.app_config import (
    deadline_has_passed,
    load_app_settings,
    local_today,
)
from schedule_adjustment_tool.domain.auth import (
    Principal,
    can_access_project,
    maintenance_mode_enabled,
)
from schedule_adjustment_tool.domain.models import (
    Config,
    Participant,
    WEEKDAY_LABELS,
    eligible_dates,
    now_iso,
    parse_slot_key,
    participant_response_editable,
)
from schedule_adjustment_tool.storage import (
    StorageConflictError,
    StorageError,
    ensure_projects,
    load_confirmed_candidate,
    load_participant_view_data,
    load_participant_workspace_data,
    list_project_participant_options,
    save_participant_responses,
)
from schedule_adjustment_tool.storage.performance import log_storage_event
from schedule_adjustment_tool.ui.availability_grid_component import (
    availability_grid,
    availability_grid_actions,
)
from schedule_adjustment_tool.ui.participant_submission_confirmation_component import (
    participant_submission_confirmation,
)
from schedule_adjustment_tool.ui.participant.confirmed_schedule import (
    ConfirmedScheduleServices,
    participant_in_session,
    render_confirmed_schedule as render_confirmed_schedule_view,
)
from schedule_adjustment_tool.ui.sidebar_navigation import (
    SidebarMenuItem,
    render_sidebar_menu,
)
from schedule_adjustment_tool.ui.authentication import (
    render_authentication as render_shared_authentication,
)
from schedule_adjustment_tool.ui.application_metadata import APP_NAME, PAGE_ICON
from schedule_adjustment_tool.ui.formatting import (
    format_date_with_weekday,
    format_datetime_with_weekday,
)
from schedule_adjustment_tool.ui.presentation import INPUT_STATUS_LABELS, STATUS_LABELS
from schedule_adjustment_tool.ui.styles import shared_page_styles


APP_SETTINGS = load_app_settings()
LOGGER = logging.getLogger("schedule_adjustment_tool")


def configure_participant_page(*, include_page_config: bool = True) -> None:
    if include_page_config:
        st.set_page_config(
            page_title=f"{APP_NAME} 参加者アプリ",
            page_icon=PAGE_ICON,
            layout="wide",
        )
    st.markdown(shared_page_styles(), unsafe_allow_html=True)
PROJECT_LIST_CACHE_KEY = "participant_project_list_cache"
VIEW_DATA_CACHE_KEY = "participant_view_data_cache"
PARTICIPANT_OPTIONS_CACHE_KEY = "participant_options_cache"
WORKSPACE_DATA_CACHE_KEY = "participant_workspace_data_cache"
PROJECT_SAVE_ACTION_SUBMIT = "submit"
PROJECT_SAVE_ACTION_DRAFT = "draft"
PROJECT_SAVE_ACTION_SKIP = "skip"
PROJECT_SAVE_ACTION_LABELS = {
    PROJECT_SAVE_ACTION_SUBMIT: "提出",
    PROJECT_SAVE_ACTION_DRAFT: "下書き",
    PROJECT_SAVE_ACTION_SKIP: "更新しない",
}
COMMON_INPUT_PERIODS = [1, 2, 3, 4, 5, 6]
PARTICIPANT_MENU_ITEMS = (
    SidebarMenuItem(
        "overview",
        "企画概要",
        ":material/info:",
    ),
    SidebarMenuItem(
        "availability",
        "参加可能日時の入力",
        ":material/edit_calendar:",
    ),
    SidebarMenuItem(
        "confirmed",
        "確定日程",
        ":material/event_available:",
    ),
)


@dataclass
class ParticipantProjectContext:
    project_id: str
    title: str
    config: Config
    participant: Participant
    confirmed: dict[str, Any] | None = None


def _defensive_participant_copy(participant: Participant) -> Participant:
    """Copy a normalized participant without sharing mutable response data."""

    copied = copy(participant)
    copied.availability = list(participant.availability)
    copied.zoom_availability = list(participant.zoom_availability)
    copied.participant_response = (
        deepcopy(participant.participant_response)
        if participant.participant_response
        else {}
    )
    copied.manager_response = (
        deepcopy(participant.manager_response)
        if participant.manager_response
        else {}
    )
    return copied


def participant_self_response_view(participant: Participant) -> Participant:
    view = _defensive_participant_copy(participant)
    response = (
        participant.participant_response
        if participant.participant_response
        else {
            "availability": participant.availability,
            "zoom_availability": participant.zoom_availability,
            "support_requested_count": participant.support_requested_count,
            "submitted_at": participant.submitted_at,
            "input_status": participant.input_status,
            "updated_at": participant.updated_at,
        }
    )
    view.availability = sorted(
        {str(value) for value in response.get("availability", [])}
    )
    view.zoom_availability = sorted(
        {
            str(value)
            for value in response.get("zoom_availability", [])
        }
        - set(view.availability)
    )
    support_requested = response.get("support_requested_count")
    view.support_requested_count = (
        None
        if support_requested in (None, "")
        else max(0, int(support_requested))
    )
    view.submitted_at = str(response.get("submitted_at", ""))
    view.input_status = str(response.get("input_status", "not_started"))
    view.updated_at = str(response.get("updated_at", ""))
    return view


def format_short_date_with_weekday(value: str | date) -> str:
    day = date.fromisoformat(value) if isinstance(value, str) else value
    return f"{day.month}/{day.day}({WEEKDAY_LABELS[day.weekday()]})"


def active_project_marker_style(
    row: pd.Series, active_project_days: set[date]
) -> list[str]:
    styles = [""] * len(row)
    if "対象期間" not in row.index:
        return styles
    try:
        day = date.fromisoformat(str(row["日付"])[:10])
    except ValueError:
        return styles
    if day not in active_project_days:
        return styles
    styles[list(row.index).index("対象期間")] = (
        "background-color: rgba(46, 204, 113, 0.55);"
    )
    return styles


@st.dialog("提出完了")
def submission_completed_dialog(message: dict[str, list[str]]) -> None:
    submitted_titles = message.get("submitted_titles", [])
    draft_titles = message.get("draft_titles", [])
    skipped_titles = message.get("skipped_titles", [])
    blocked_titles = message.get("blocked_titles", [])
    updated_titles = message.get("updated_titles", [])
    if updated_titles and not submitted_titles and not draft_titles:
        submitted_titles = updated_titles
    if submitted_titles:
        st.success("回答を提出しました。")
        st.write("提出として更新した企画: " + "、".join(submitted_titles))
    if draft_titles:
        st.info("下書きとして保存した企画: " + "、".join(draft_titles))
    if skipped_titles:
        st.write("今回は更新しなかった企画: " + "、".join(skipped_titles))
    if blocked_titles:
        st.warning("更新不可の企画: " + "、".join(blocked_titles))
    if st.button("閉じる", type="primary"):
        st.session_state.pop("participant_submission_dialog", None)
        st.rerun()


def resolved_project_save_actions(
    contexts: list[ParticipantProjectContext],
    selected_actions: dict[str, str],
    *,
    submit_all: bool,
) -> dict[str, str]:
    """Resolve the exact per-project action before showing the confirmation."""

    actions: dict[str, str] = {}
    for context in contexts:
        options = project_save_action_options(context)
        if submit_all:
            action = (
                PROJECT_SAVE_ACTION_SUBMIT
                if PROJECT_SAVE_ACTION_SUBMIT in options
                else PROJECT_SAVE_ACTION_SKIP
            )
        else:
            action = selected_actions.get(
                context.project_id, PROJECT_SAVE_ACTION_SKIP
            )
        actions[context.project_id] = (
            action if action in options else PROJECT_SAVE_ACTION_SKIP
        )
    return actions


def project_submission_summary(
    contexts: list[ParticipantProjectContext],
    actions: dict[str, str],
) -> dict[str, list[str]]:
    summary = {
        "submitted_titles": [],
        "draft_titles": [],
        "skipped_titles": [],
        "blocked_titles": [],
    }
    for context in contexts:
        action = actions.get(context.project_id, PROJECT_SAVE_ACTION_SKIP)
        if action == PROJECT_SAVE_ACTION_SUBMIT:
            summary["submitted_titles"].append(context.title)
        elif action == PROJECT_SAVE_ACTION_DRAFT:
            summary["draft_titles"].append(context.title)
        elif (
            context.config.status == "confirmed"
            or not participant_input_editable(context.config)
        ):
            summary["blocked_titles"].append(context.title)
        else:
            summary["skipped_titles"].append(context.title)
    return summary


def save_project_responses(
    contexts: list[ParticipantProjectContext],
    selected_slots: set[str],
    selected_zoom_slots: set[str],
    actions: dict[str, str],
) -> dict[str, list[str]]:
    """Apply a previously reviewed plan in one storage transaction."""

    summary = project_submission_summary(contexts, actions)
    timestamp = now_iso()
    responses: list[tuple[str, Participant]] = []
    prepared_participants: list[tuple[ParticipantProjectContext, Participant]] = []
    for context in contexts:
        save_action = actions.get(
            context.project_id, PROJECT_SAVE_ACTION_SKIP
        )
        if save_action == PROJECT_SAVE_ACTION_SKIP:
            continue
        participant = _defensive_participant_copy(context.participant)
        participant.availability = project_availability_subset(
            context.config, selected_slots
        )
        participant.zoom_availability = project_availability_subset(
            context.config, selected_zoom_slots
        )
        participant.input_status = (
            "submitted"
            if save_action == PROJECT_SAVE_ACTION_SUBMIT
            else "draft"
        )
        participant.updated_at = timestamp
        if save_action == PROJECT_SAVE_ACTION_SUBMIT:
            participant.submitted_at = timestamp
        responses.append((context.project_id, participant))
        prepared_participants.append((context, participant))

    # A participant often submits the same selection to several projects.  A
    # single transaction prevents a conflict or failure in a later project
    # from leaving the earlier projects saved while the UI reports one result.
    save_started = time_module.perf_counter()
    context_by_project = {context.project_id: context for context in contexts}
    save_participant_responses(
        responses,
        expected_config_versions={
            project_id: int(
                getattr(
                    context_by_project[project_id].config,
                    "_storage_version",
                    0,
                )
            )
            for project_id, _participant in responses
        },
    )
    for context, participant in prepared_participants:
        context.participant = participant
    database_elapsed = time_module.perf_counter() - save_started
    cache_started = time_module.perf_counter()
    affected_project_ids = {
        context.project_id
        for context in contexts
        if actions.get(context.project_id, PROJECT_SAVE_ACTION_SKIP)
        != PROJECT_SAVE_ACTION_SKIP
    }
    participant_id = contexts[0].participant.id if contexts else ""
    update_participant_caches_after_save(
        participant_id,
        contexts,
        affected_project_ids,
    )
    cache_elapsed = time_module.perf_counter() - cache_started
    LOGGER.info(
        "participant_response_save_timing response_count=%d project_count=%d "
        "db_and_snapshot_seconds=%.4f cache_seconds=%.4f "
        "cache_action=replace",
        len(responses),
        len(affected_project_ids),
        database_elapsed,
        cache_elapsed,
    )
    return summary


@st.dialog("保存内容の最終確認")
def participant_submission_confirmation_dialog(
    contexts: list[ParticipantProjectContext],
    selected_slots: set[str],
    selected_zoom_slots: set[str],
    actions: dict[str, str],
) -> None:
    summary = project_submission_summary(contexts, actions)
    st.caption(
        "保存前の確認です。企画ごとの扱いを確認してください。"
    )
    if summary["submitted_titles"]:
        st.warning(
            "提出として保存: " + "、".join(summary["submitted_titles"])
        )
    if summary["draft_titles"]:
        st.info(
            "下書きとして保存: " + "、".join(summary["draft_titles"])
        )
    if summary["skipped_titles"]:
        st.write(
            "今回は更新しない: " + "、".join(summary["skipped_titles"])
        )
    if summary["blocked_titles"]:
        st.write(
            "受付期間外・確定済みのため更新しない: "
            + "、".join(summary["blocked_titles"])
        )
    st.write(
        f"選択中の参加可能コマ: {len(selected_slots)} / "
        f"うちZoomなら可: {len(selected_zoom_slots)}"
    )
    has_updates = bool(
        summary["submitted_titles"] or summary["draft_titles"]
    )
    participant_names = sorted(
        {
            context.participant.name.strip()
            for context in contexts
            if context.participant.name.strip()
        }
    )
    participant_name = participant_names[0] if len(participant_names) == 1 else ""
    confirmation_key = (
        "participant_submission_confirm_name_"
        + "_".join(sorted(context.project_id for context in contexts))
    )
    if participant_name:
        st.warning(
            f"共有アカウントで操作中です。参加者「{participant_name}」本人の"
            "入力であることを確認してください。"
        )
    else:
        st.error("提出対象の参加者を一意に確認できないため、保存できません。")
    if not has_updates:
        st.warning(
            "保存対象の企画がありません。戻って今回の扱いを選び直してください。"
        )
    initial_confirmation = str(
        st.session_state.get(confirmation_key, "") or ""
    ).strip()
    component_value = participant_submission_confirmation(
        participant_name,
        initial_value=initial_confirmation,
        has_updates=has_updates and bool(participant_name),
        max_chars=APP_SETTINGS.max_text_length,
        state_id=f"{confirmation_key}_{participant_name}",
        key=(
            "participant_submission_confirmation_"
            + "_".join(sorted(context.project_id for context in contexts))
        ),
    )
    component_action = ""
    confirmation = initial_confirmation
    if isinstance(component_value, dict):
        component_action = str(component_value.get("action", ""))
        confirmation = str(
            component_value.get("confirmation", "") or ""
        ).strip()
        st.session_state[confirmation_key] = confirmation
        nonce = str(component_value.get("nonce", "") or "")
        handled_nonce_key = f"{confirmation_key}_handled_nonce"
        if nonce and st.session_state.get(handled_nonce_key) == nonce:
            component_action = ""
        elif component_action and nonce:
            st.session_state[handled_nonce_key] = nonce

    if component_action == "back":
        st.rerun()
    if component_action == "save":
        if not participant_name:
            st.error("参加者名を一意に確認できないため、保存を実行できません。")
            return
        if not has_updates:
            st.error("保存対象の企画がないため、保存を実行できません。")
            return
        if confirmation != participant_name:
            st.error("参加者名が一致しないため、保存を実行できません。")
            return
        try:
            with st.spinner("回答を保存しています...", show_time=True):
                st.session_state["participant_submission_dialog"] = (
                    save_project_responses(
                        contexts,
                        selected_slots,
                        selected_zoom_slots,
                        actions,
                    )
                )
        except (StorageConflictError, StorageError) as error:
            st.error(str(error))
        else:
            st.session_state["participant_save_rerender_started_at"] = (
                time_module.perf_counter()
            )
            st.rerun()


def render_save_notice() -> None:
    message = st.session_state.pop("participant_save_notice", None)
    if message:
        st.success(str(message))


def render_authentication() -> Principal:
    return render_shared_authentication()


def load_project_list_cached(*, force: bool = False) -> list[dict]:
    if force or PROJECT_LIST_CACHE_KEY not in st.session_state:
        st.session_state[PROJECT_LIST_CACHE_KEY] = ensure_projects()
    return st.session_state[PROJECT_LIST_CACHE_KEY]


def view_cache() -> dict[tuple[str, str, bool, bool], dict]:
    return st.session_state.setdefault(VIEW_DATA_CACHE_KEY, {})


def participant_options_cache() -> dict[str, list[dict[str, Any]]]:
    return st.session_state.setdefault(PARTICIPANT_OPTIONS_CACHE_KEY, {})


def load_participant_options_cached(
    project_id: str,
    *,
    force: bool = False,
) -> list[dict[str, Any]]:
    cache = participant_options_cache()
    if force or project_id not in cache:
        cache[project_id] = list_project_participant_options(project_id)
    return cache[project_id]


def workspace_data_cache() -> dict[tuple[str, tuple[str, ...], bool], dict]:
    return st.session_state.setdefault(WORKSPACE_DATA_CACHE_KEY, {})


def load_workspace_data_cached(
    participant_id: str,
    project_ids: list[str],
    *,
    include_confirmed: bool = False,
    force: bool = False,
) -> dict[str, dict[str, Any]]:
    key = (str(participant_id), tuple(project_ids), include_confirmed)
    cache = workspace_data_cache()
    if force or key not in cache:
        cache[key] = load_participant_workspace_data(
            participant_id,
            project_ids,
            include_confirmed=include_confirmed,
        )
    return cache[key]


def load_view_data_cached(
    project_id: str,
    *,
    participant_id: str,
    include_all_participants: bool,
    include_confirmed: bool = False,
    force: bool = False,
) -> dict:
    key = (project_id, participant_id, include_all_participants, include_confirmed)
    cache = view_cache()
    if force or key not in cache:
        cache[key] = load_participant_view_data(
            project_id,
            participant_id=participant_id,
            include_all_participants=include_all_participants,
            include_confirmed=include_confirmed,
        )
    return cache[key]


def clear_view_cache(project_id: str) -> None:
    cache = view_cache()
    for key in list(cache):
        if key[0] == project_id:
            cache.pop(key, None)


def clear_workspace_data_cache(
    participant_id: str,
    *,
    project_ids: set[str] | None = None,
) -> None:
    """Drop saved workspace data after a response update."""

    cache = workspace_data_cache()
    participant_id = str(participant_id)
    for key in list(cache):
        cached_participant_id, cached_project_ids, _include_confirmed = key
        if cached_participant_id != participant_id:
            continue
        if project_ids is not None and not project_ids.intersection(
            cached_project_ids
        ):
            continue
        cache.pop(key, None)


def update_participant_caches_after_save(
    participant_id: str,
    contexts: list[ParticipantProjectContext],
    affected_project_ids: set[str],
) -> None:
    """Replace saved participant data in session caches without a read rerun."""

    participant_id = str(participant_id)
    by_project = {
        context.project_id: context
        for context in contexts
        if context.project_id in affected_project_ids
    }
    if not by_project:
        return

    for key, data in list(view_cache().items()):
        project_id, cached_id, include_all_participants, _include_confirmed = key
        if str(cached_id) != participant_id or project_id not in by_project:
            continue
        context = by_project[project_id]
        participant = _defensive_participant_copy(context.participant)
        if include_all_participants:
            participants = list(data.get("participants", []))
            replaced = False
            for index, current in enumerate(participants):
                if str(current.id) == participant_id:
                    participants[index] = participant
                    replaced = True
                    break
            if not replaced:
                participants.append(participant)
            data["participants"] = participants
        else:
            data["participants"] = [participant]
        data["config"] = context.config

    for key, data in list(workspace_data_cache().items()):
        cached_id, cached_project_ids, _include_confirmed = key
        if str(cached_id) != participant_id:
            continue
        for project_id, context in by_project.items():
            if project_id not in cached_project_ids:
                continue
            current = data.get(project_id)
            if current is None:
                continue
            current["config"] = context.config
            current["participants"] = [
                _defensive_participant_copy(context.participant)
            ]


def find_participant(
    participants: list[Participant], participant_id: str
) -> Participant | None:
    return next(
        (
            participant
            for participant in participants
            if participant.id == participant_id
        ),
        None,
    )


def participant_name(participants: list[Participant], participant_id: str) -> str:
    participant = find_participant(participants, participant_id)
    return participant.name if participant else "不明な参加者"


def project_title(projects: list[dict], project_id: str) -> str:
    return next(
        (
            str(project.get("title", "名称未設定"))
            for project in projects
            if str(project["id"]) == project_id
        ),
        "名称未設定",
    )


def config_input_dates(config: Config) -> list[date]:
    try:
        return eligible_dates(config)
    except ValueError:
        return []


def date_grid_with_gaps(days: set[date]) -> tuple[list[date], set[date]]:
    ordered = sorted(days)
    if not ordered:
        return [], set()
    gap_starts = {
        day
        for previous, day in zip(ordered, ordered[1:])
        if day > previous + timedelta(days=1)
    }
    return ordered, gap_starts


def merged_availability(contexts: list[ParticipantProjectContext]) -> set[str]:
    slots: set[str] = set()
    for context in contexts:
        slots.update(context.participant.availability)
    return slots


def merged_zoom_availability(contexts: list[ParticipantProjectContext]) -> set[str]:
    slots: set[str] = set()
    for context in contexts:
        slots.update(context.participant.zoom_availability)
    return slots


def participant_input_editable(config: Config) -> bool:
    return participant_response_editable(config)


def participant_visible_project_status(config: Config) -> str:
    """Show a collecting project as closed once its deadline has passed."""

    if config.status != "collecting" or not config.response_deadline:
        return config.status
    if deadline_has_passed(config.response_deadline):
        return "closed"
    return config.status


def project_save_action_options(context: ParticipantProjectContext) -> list[str]:
    if context.config.status == "confirmed":
        return [PROJECT_SAVE_ACTION_SKIP]
    if not participant_input_editable(context.config):
        return [PROJECT_SAVE_ACTION_SKIP]
    return [
        PROJECT_SAVE_ACTION_SUBMIT,
        PROJECT_SAVE_ACTION_DRAFT,
        PROJECT_SAVE_ACTION_SKIP,
    ]


def default_project_save_action(context: ParticipantProjectContext) -> str:
    options = project_save_action_options(context)
    if options == [PROJECT_SAVE_ACTION_SKIP]:
        return PROJECT_SAVE_ACTION_SKIP
    if context.participant.input_status == "submitted":
        return PROJECT_SAVE_ACTION_SUBMIT
    return PROJECT_SAVE_ACTION_DRAFT


def render_project_submission_controls(
    contexts: list[ParticipantProjectContext],
    participant: Participant,
) -> dict[str, str]:
    st.markdown(
        """
<style>
.submission-control-table {
    width: 100%;
    max-width: none;
    font-size: 0.88rem;
    line-height: 1.25;
    border-top: 1px solid rgba(128, 128, 128, 0.35);
    border-left: 1px solid rgba(128, 128, 128, 0.35);
    border-radius: 4px;
    overflow: hidden;
}
.submission-control-table [data-testid="stHorizontalBlock"] {
    gap: 0;
    border-bottom: 1px solid rgba(128, 128, 128, 0.35);
}
.submission-control-table p {
    margin: 0.12rem 0;
}
.submission-control-table label {
    min-height: 0;
}
.submission-control-table .element-container {
    margin: 0;
}
.submission-control-table [data-testid="column"] {
    min-height: 2.15rem;
    padding: 0.2rem 0.32rem;
    border-right: 1px solid rgba(128, 128, 128, 0.35);
    display: flex;
    align-items: center;
}
.submission-control-table .submission-header [data-testid="column"] {
    min-height: 1.55rem;
    background: rgba(128, 128, 128, 0.08);
    font-weight: 650;
}
.submission-control-table [data-testid="stSegmentedControl"] {
    width: 100%;
}
.submission-control-table [data-testid="stSegmentedControl"] label {
    padding: 0.05rem 0.18rem;
    font-size: 0.62rem;
    line-height: 1.1;
}
.submission-cell-text {
    color: inherit !important;
    font-size: 0.8rem;
    font-weight: 500;
    line-height: 1.2;
    opacity: 1 !important;
    -webkit-text-fill-color: currentColor !important;
}
.submission-cell-text.active {
    color: inherit !important;
    font-weight: 500 !important;
    opacity: 1 !important;
    -webkit-text-fill-color: currentColor !important;
}
.submission-project-tag {
    display: inline-block;
    max-width: 100%;
    padding: 1px 6px;
    border-radius: 999px;
    background: rgba(46, 204, 113, 0.14);
    color: #16794a;
    font-size: 0.78rem;
    font-weight: 650;
    overflow-wrap: anywhere;
    white-space: normal;
    vertical-align: middle;
}
.submission-project-tag.color-0 {
    background: rgba(25, 118, 210, 0.12);
    color: #0d47a1;
}
.submission-project-tag.color-1 {
    background: rgba(46, 125, 50, 0.12);
    color: #1b5e20;
}
.submission-project-tag.color-2 {
    background: rgba(239, 108, 0, 0.14);
    color: #a04000;
}
.submission-project-tag.color-3 {
    background: rgba(123, 31, 162, 0.12);
    color: #4a148c;
}
.submission-project-tag.color-4 {
    background: rgba(0, 121, 107, 0.12);
    color: #004d40;
}
.submission-project-tag.color-5 {
    background: rgba(198, 40, 40, 0.12);
    color: #8e0000;
}
.submission-project-tag.color-6 {
    background: rgba(69, 90, 100, 0.13);
    color: #263238;
}
.submission-project-tag.color-7 {
    background: rgba(194, 24, 91, 0.12);
    color: #880e4f;
}
.submission-project-tag.color-8 {
    background: rgba(85, 139, 47, 0.13);
    color: #33691e;
}
.submission-project-tag.color-9 {
    background: rgba(94, 53, 177, 0.12);
    color: #311b92;
}
.submission-project-tag.active {
    border: 1px solid currentColor;
    box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.7);
}
.submission-project-tag.active.color-0 {
    background: #dbeafe;
    color: #1d4ed8;
}
.submission-project-tag.active.color-1 {
    background: #dcfce7;
    color: #166534;
}
.submission-project-tag.active.color-2 {
    background: #ffedd5;
    color: #9a3412;
}
.submission-project-tag.active.color-3 {
    background: #f3e8ff;
    color: #6b21a8;
}
.submission-project-tag.active.color-4 {
    background: #ccfbf1;
    color: #115e59;
}
.submission-project-tag.active.color-5 {
    background: #fee2e2;
    color: #991b1b;
}
.submission-project-tag.active.color-6 {
    background: #e2e8f0;
    color: #334155;
}
.submission-project-tag.active.color-7 {
    background: #fce7f3;
    color: #9d174d;
}
.submission-project-tag.active.color-8 {
    background: #ecfccb;
    color: #3f6212;
}
.submission-project-tag.active.color-9 {
    background: #ede9fe;
    color: #5b21b6;
}
</style>
        """.strip(),
        unsafe_allow_html=True,
    )
    st.markdown("##### 提出確認")
    st.caption(
        "企画ごとに「提出・下書き・更新しない」を選び、"
        "保存ボタンの後に表示される最終確認で内容を確定します。"
    )
    selected_project_actions: dict[str, str] = {}
    color_indexes = project_chip_color_indexes(contexts)
    with st.container():
        st.markdown('<div class="submission-control-table">', unsafe_allow_html=True)
        st.markdown('<div class="submission-header">', unsafe_allow_html=True)
        header_columns = st.columns([1.55, 1.15, 0.75, 0.85, 2.2])
        header_columns[0].caption("企画")
        header_columns[1].caption("期間")
        header_columns[2].caption("現在")
        header_columns[3].caption("状態")
        header_columns[4].caption("今回")
        st.markdown("</div>", unsafe_allow_html=True)
        for context in contexts:
            options = project_save_action_options(context)
            default_action = default_project_save_action(context)
            action_key = (
                f"participant_project_save_action_"
                f"{participant.id}_{context.project_id}"
            )
            if st.session_state.get(action_key) not in options:
                st.session_state[action_key] = default_action
            columns = st.columns([1.55, 1.15, 0.75, 0.85, 2.2])
            columns[0].markdown(
                project_chip_html(
                    context.title,
                    color_indexes.get(context.title, 0),
                    active=True,
                ),
                unsafe_allow_html=True,
            )
            columns[1].markdown(
                submission_cell_html(
                    f"{format_short_date_with_weekday(context.config.start_date)}〜"
                    f"{format_short_date_with_weekday(context.config.end_date)}",
                    active=True,
                ),
                unsafe_allow_html=True,
            )
            columns[2].markdown(
                submission_cell_html(
                    INPUT_STATUS_LABELS.get(
                        context.participant.input_status,
                        context.participant.input_status,
                    ),
                    active=True,
                ),
                unsafe_allow_html=True,
            )
            columns[3].markdown(
                submission_cell_html(
                    STATUS_LABELS.get(
                        participant_visible_project_status(context.config),
                        participant_visible_project_status(context.config),
                    ),
                    active=True,
                ),
                unsafe_allow_html=True,
            )
            selected_project_actions[context.project_id] = columns[4].segmented_control(
                "今回の扱い",
                options,
                key=action_key,
                format_func=lambda value: PROJECT_SAVE_ACTION_LABELS[value],
                label_visibility="collapsed",
                disabled=len(options) == 1,
            ) or st.session_state[action_key]
        st.markdown("</div>", unsafe_allow_html=True)
    return selected_project_actions


def render_availability_input_guide() -> None:
    st.caption(
        "通常日は各コマのチェックで参加可/不可を切り替えます。"
        "「Zoomなら可」をONにした日は、チェック済みコマがZoom可になり、"
        "Zoom/対面ラベルで参加方法を切り替えます。"
    )


def editable_contexts(
    contexts: list[ParticipantProjectContext],
) -> list[ParticipantProjectContext]:
    return [context for context in contexts if participant_input_editable(context.config)]


def visible_input_dates(contexts: list[ParticipantProjectContext]) -> set[date]:
    today = local_today()
    days: set[date] = set()
    for context in contexts:
        editable = participant_input_editable(context.config)
        days.update(
            day
            for day in config_input_dates(context.config)
            if day >= today or editable
        )
    return days


def project_titles_by_input_day(
    contexts: list[ParticipantProjectContext],
) -> dict[date, list[str]]:
    titles_by_day: dict[date, list[str]] = {}
    for context in contexts:
        for day in config_input_dates(context.config):
            titles_by_day.setdefault(day, []).append(context.title)
    return {
        day: sorted(set(titles))
        for day, titles in titles_by_day.items()
    }


def editable_project_titles_by_input_day(
    contexts: list[ParticipantProjectContext],
) -> dict[date, list[str]]:
    """Return only projects whose response can be submitted on each date."""

    titles_by_day: dict[date, list[str]] = {}
    for context in editable_contexts(contexts):
        for day in config_input_dates(context.config):
            titles_by_day.setdefault(day, []).append(context.title)
    return {
        day: sorted(set(titles))
        for day, titles in titles_by_day.items()
    }


def project_chip_color_indexes(
    contexts: list[ParticipantProjectContext],
) -> dict[str, int]:
    indexes: dict[str, int] = {}
    next_index = 0
    for day in sorted(project_titles_by_input_day(contexts)):
        for title in project_titles_by_input_day(contexts)[day]:
            if title not in indexes:
                indexes[title] = next_index % 10
                next_index += 1
    for context in contexts:
        if context.title not in indexes:
            indexes[context.title] = next_index % 10
            next_index += 1
    return indexes


def project_chip_html(
    title: str,
    color_index: int,
    *,
    active: bool = False,
) -> str:
    active_class = " active" if active else ""
    return (
        f"<span class='submission-project-tag color-{color_index}{active_class}' "
        f"title='{html.escape(title, quote=True)}'>"
        f"{html.escape(title)}"
        "</span>"
    )


def submission_cell_html(value: str, *, active: bool = False) -> str:
    active_class = " active" if active else ""
    return (
        f"<span class='submission-cell-text{active_class}'>"
        f"{html.escape(value)}</span>"
    )


def project_availability_subset(config: Config, availability: set[str]) -> list[str]:
    project_days = set(config_input_dates(config))
    enabled_periods = {
        int(period)
        for period in config.enabled_periods
        if str(period).isdigit()
    }
    selected: list[str] = []
    for slot_key in availability:
        try:
            day, period = parse_slot_key(slot_key)
        except (ValueError, TypeError):
            continue
        if day in project_days and period in enabled_periods:
            selected.append(slot_key)
    return sorted(selected)


def principal_participant_ids(principal: Principal) -> list[str]:
    ids: list[str] = []
    for membership in principal.memberships:
        if membership.get("role") != "participant":
            continue
        participant_id = str(membership.get("participant_id") or "")
        if participant_id and participant_id not in ids:
            ids.append(participant_id)
    return ids


def project_participant_for_principal(
    principal: Principal, project_id: str
) -> str:
    direct_participant_id = principal.participant_id(project_id)
    if direct_participant_id:
        return direct_participant_id
    if (
        not principal.authentication_enabled
        or principal.can_select_all_participants(project_id)
    ):
        return ""
    for participant_id in principal_participant_ids(principal):
        data = load_view_data_cached(
            project_id,
            participant_id=participant_id,
            include_all_participants=False,
        )
        if any(
            participant.active and participant.id == participant_id
            for participant in data["participants"]
        ):
            return participant_id
    return ""


def selectable_project_ids(principal: Principal) -> list[dict]:
    projects = []
    for project in load_project_list_cached():
        if project.get("archived", False):
            continue
        project_id = str(project["id"])
        if can_access_project(principal, project_id, "参加者"):
            projects.append(project)
            continue
        if project_participant_for_principal(principal, project_id):
            projects.append(project)
    return projects


def participant_project_contexts(
    projects: list[dict],
    participant_id: str,
    *,
    workspace_data: dict[str, dict[str, Any]] | None = None,
    include_confirmed: bool = False,
    force: bool = False,
) -> list[ParticipantProjectContext]:
    contexts: list[ParticipantProjectContext] = []
    for project in projects:
        project_id = str(project["id"])
        if workspace_data is None:
            data = load_view_data_cached(
                project_id,
                participant_id=participant_id,
                include_all_participants=False,
                include_confirmed=include_confirmed,
                force=force,
            )
        else:
            data = workspace_data.get(project_id)
            if data is None:
                continue
        participants = [
            participant
            for participant in data["participants"]
            if participant.active and participant.id == participant_id
        ]
        if not participants:
            continue
        contexts.append(
            ParticipantProjectContext(
                project_id=project_id,
                title=str(project.get("title", data["config"].title)),
                config=data["config"],
                participant=participant_self_response_view(participants[0]),
                confirmed=data.get("confirmed"),
            )
        )
    return contexts


def render_overview(config: Config, participant: Participant | None) -> None:
    st.header("企画概要")
    visible_status = participant_visible_project_status(config)
    st.caption(f"{config.title} | {STATUS_LABELS.get(visible_status, visible_status)}")
    if config.description:
        st.write(config.description)
    st.info(
        f"対象期間: {format_date_with_weekday(config.start_date)} ～ "
        f"{format_date_with_weekday(config.end_date)} / "
        f"入力締切: "
        f"{format_datetime_with_weekday(config.response_deadline) or '未設定'}"
    )
    if participant:
        label = INPUT_STATUS_LABELS.get(
            participant.input_status, participant.input_status
        )
        st.success(f"あなたの入力状態: {label}")
        if participant.response_source == "manager":
            st.info(
                "この企画では、担当者が調整した回答を日程作成に使用しています。"
                "あなたの入力も別に保存されており、担当者が調整を解除すると"
                "再び日程作成に使用されます。"
            )


def render_availability_form(
    contexts: list[ParticipantProjectContext],
    participant: Participant,
    active_project_id: str,
) -> None:
    st.header("参加可能日時の入力")
    manager_override_titles = [
        context.title
        for context in contexts
        if context.participant.response_source == "manager"
    ]
    if manager_override_titles:
        st.info(
            "次の企画では担当者が調整した回答を日程作成に使用しています: "
            + "、".join(manager_override_titles)
            + "。ここで保存したあなたの入力も別に保持され、"
            "担当者が調整を解除した時に使用されます。"
        )
    project_names = "、".join(context.title for context in contexts)
    st.info(f"あなたは次の企画に参加予定です: {project_names}")
    active_context = next(
        (
            context
            for context in contexts
            if context.project_id == active_project_id
        ),
        None,
    )
    editable_context_list = editable_contexts(contexts)
    input_open = bool(editable_context_list)
    if not input_open:
        st.warning(
            "選択中の企画は回答受付期間外です。"
            "回答内容は閲覧できますが、変更や新しい回答はできません。"
        )
    editable_titles_by_day = editable_project_titles_by_input_day(contexts)
    active_project_days = set(editable_titles_by_day)
    if active_context and active_project_days and input_open:
        st.caption(
            "日付欄には、その日が対象期間に含まれる企画名を表示します。"
        )
    elif active_context:
        st.caption(
            f"選択中の企画「{active_context.title}」の回答内容を表示しています。"
        )
    statuses = {
        INPUT_STATUS_LABELS.get(
            context.participant.input_status, context.participant.input_status
        )
        for context in contexts
    }
    st.caption(
        f"{participant.name} / 入力状態: {'、'.join(sorted(statuses))}"
    )

    availability = merged_availability(contexts)
    zoom_availability = merged_zoom_availability(contexts)
    visible_days, gap_days = date_grid_with_gaps(visible_input_dates(contexts))
    if not visible_days:
        st.info("現在表示できる入力対象期間はありません。")
        return

    render_availability_input_guide()
    grid_key = f"participant_availability_{participant.id}"
    availability_grid(
        visible_days,
        periods=COMMON_INPUT_PERIODS,
        availability=availability,
        zoom_availability=zoom_availability,
        key=grid_key,
        disabled=not input_open,
        active_project_days=active_project_days,
        project_titles_by_day=project_titles_by_input_day(contexts),
        active_project_titles_by_day=editable_titles_by_day,
        gap_days=gap_days,
        show_actions=False,
    )

    selected_project_actions = render_project_submission_controls(
        contexts, participant
    )
    selected_slots, selected_zoom_slots, grid_action = availability_grid_actions(
        visible_days,
        periods=COMMON_INPUT_PERIODS,
        availability=availability,
        zoom_availability=zoom_availability,
        key=f"{grid_key}_actions",
        storage_key=grid_key,
        disabled=not input_open,
        gap_days=gap_days,
        action_buttons=[
            {
                "action": "save_selected",
                "label": "選択した扱いで保存",
                "primary": True,
            },
            {
                "action": "submit_all",
                "label": "すべて提出として保存",
                "primary": False,
            },
        ],
    )
    save_selected_clicked = grid_action == "save_selected"
    submit_all_clicked = grid_action == "submit_all"
    if not (save_selected_clicked or submit_all_clicked):
        return

    actions = resolved_project_save_actions(
        contexts,
        selected_project_actions,
        submit_all=submit_all_clicked,
    )
    participant_submission_confirmation_dialog(
        contexts,
        set(selected_slots),
        set(selected_zoom_slots),
        actions,
    )


def _confirmed_schedule_services() -> ConfirmedScheduleServices:
    """Bind participant entrypoint storage and local date formatting."""

    return ConfirmedScheduleServices(
        load_confirmed_candidate=load_confirmed_candidate,
        format_datetime=format_datetime_with_weekday,
        format_date=format_date_with_weekday,
    )


def render_confirmed_schedule(
    project_id: str, config: Config, participant: Participant
) -> None:
    """Compatibility entrypoint for the participant confirmed-schedule view."""

    render_confirmed_schedule_view(
        project_id,
        config,
        participant,
        services=_confirmed_schedule_services(),
    )


def render_participant_workspace(principal: Principal) -> None:
    """Render the current participant workflow inside either entry point."""

    rerender_started_at = st.session_state.pop(
        "participant_save_rerender_started_at", None
    )
    if isinstance(rerender_started_at, (float, int)):
        rerender_elapsed = time_module.perf_counter() - float(
            rerender_started_at
        )
        if rerender_elapsed >= 0:
            log_storage_event(
                LOGGER,
                "participant_response_rerender",
                rerender_seconds=round(rerender_elapsed, 6),
                total_seconds=round(rerender_elapsed, 6),
            )
    render_save_notice()
    submission_message = st.session_state.get("participant_submission_dialog")
    if isinstance(submission_message, dict):
        submission_completed_dialog(submission_message)
    projects = selectable_project_ids(principal)
    if not projects:
        st.warning("利用できる企画がありません。")
        return

    project_ids = [str(project["id"]) for project in projects]
    stored_project_id = str(
        st.session_state.get("participant_active_project_id", "") or ""
    )
    if stored_project_id not in project_ids:
        stored_project_id = ""
    unique_authenticated_participant = bool(
        principal.authentication_enabled
        and len(project_ids) == 1
        and (
            principal.is_participant
            or principal.participant_id(project_ids[0])
        )
    )
    auto_select_project = (
        unique_authenticated_participant
    )
    initial_project_id = stored_project_id or (
        project_ids[0] if auto_select_project else ""
    )
    with st.sidebar:
        st.title("日程調整")
        st.caption("参加者")
        active_project_id = st.selectbox(
            "企画",
            project_ids,
            index=(
                project_ids.index(initial_project_id)
                if initial_project_id
                else None
            ),
            placeholder="企画を選択してください",
            format_func=lambda value: project_title(projects, value),
            key="participant_active_project_id",
        )
    active_project_id = str(active_project_id or "")
    if not active_project_id:
        st.info("まず企画を選択してください。")
        return

    include_all = (
        not principal.authentication_enabled
        or principal.can_select_all_participants(active_project_id)
    )
    with st.sidebar:
        refresh_clicked = st.button(
            "参加者情報を更新",
            icon=":material/refresh:",
            width="stretch",
        )

    participant_id = (
        project_participant_for_principal(principal, active_project_id)
        if principal.authentication_enabled and not include_all
        else ""
    )
    if include_all:
        options = load_participant_options_cached(
            active_project_id,
            force=refresh_clicked,
        )
        active_options = [option for option in options if option.get("active")]
        if not active_options:
            st.info(
                "この企画の参加者は登録されていますが、"
                "日程調整対象者がまだ選択されていません。"
            )
            return
        participant_ids = [str(option["id"]) for option in active_options]
        participant_names = {
            str(option["id"]): str(option.get("name", ""))
            for option in active_options
        }
        participant_selector_key = f"participant_selector_{active_project_id}"
        stored_participant_id = str(
            st.session_state.get(participant_selector_key, "") or ""
        )
        if stored_participant_id not in participant_ids:
            stored_participant_id = ""
        auto_select_participant = (
            principal.authentication_enabled
            and principal.is_participant
            and len(participant_ids) == 1
        )
        with st.sidebar:
            selected_participant_id = st.selectbox(
                "名前",
                participant_ids,
                index=(
                    participant_ids.index(stored_participant_id)
                    if stored_participant_id
                    else 0
                    if auto_select_participant
                    else None
                ),
                placeholder="参加者を選択してください",
                format_func=lambda value: participant_names.get(value, value),
                key=participant_selector_key,
            )
        participant_id = str(selected_participant_id or "")
    if not participant_id:
        st.info("次に参加者を選択してください。")
        return

    workspace_data = load_workspace_data_cached(
        participant_id,
        project_ids,
        force=refresh_clicked,
    )
    contexts = participant_project_contexts(
        projects,
        participant_id,
        workspace_data=workspace_data,
    )
    if not contexts:
        st.info("この参加者が登録されている企画が見つかりません。")
        return
    active_data = workspace_data.get(active_project_id)
    if active_data is None:
        st.info("選択した企画の参加者情報を読み込めませんでした。")
        return
    config = active_data["config"]
    participant = next(
        (
            context.participant
            for context in contexts
            if context.project_id == active_project_id
        ),
        contexts[0].participant,
    )
    privacy_notice = str(active_data.get("privacy_notice", "") or "").strip()
    if privacy_notice:
        st.markdown("##### 参加者向けプライバシー説明")
        st.info(privacy_notice)

    with st.sidebar:
        visible_status = participant_visible_project_status(config)
        st.badge(
            STATUS_LABELS.get(visible_status, visible_status),
            icon=":material/event:",
            color=(
                "green"
                if visible_status == "collecting"
                else "blue"
                if visible_status == "confirmed"
                else "gray"
            ),
        )
        st.divider()
    view = render_sidebar_menu(
        PARTICIPANT_MENU_ITEMS,
        state_key=f"participant_view_{active_project_id}",
        default="availability",
        key_prefix=f"participant_view_{active_project_id}",
        heading="メニュー",
    )
    view_title = next(
        item.label for item in PARTICIPANT_MENU_ITEMS if item.id == view
    )
    st.caption(f"{config.title} ＞ {view_title}")
    if view == "overview":
        render_overview(config, participant)
        st.info(
            "あなたは次の企画に参加予定です: "
            + "、".join(context.title for context in contexts)
        )
    elif view == "confirmed":
        render_confirmed_schedule(active_project_id, config, participant)
    else:
        render_availability_form(contexts, participant, active_project_id)


def main() -> None:
    principal = render_authentication()
    if not principal.is_system_admin and maintenance_mode_enabled(principal):
        st.warning("メンテナンス中")
        return
    render_participant_workspace(principal)


def run(*, configure_page: bool = True) -> None:
    try:
        if configure_page:
            configure_participant_page()
        main()
    except StorageConflictError as error:
        st.error(str(error))
        st.info("他の利用者の更新を反映するため、ページを再読み込みしてください。")
    except StorageError as error:
        st.error(f"データ保存エラー: {error}")
    except Exception:
        error_id = uuid4().hex[:12]
        LOGGER.exception("Unhandled participant application error id=%s", error_id)
        st.error(
            "予期しないエラーが発生しました。"
            f" 管理者へエラーID `{error_id}` を連絡してください。"
        )


if __name__ == "__main__":
    run()
