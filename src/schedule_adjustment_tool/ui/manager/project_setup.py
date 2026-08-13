"""Project metadata and response-window settings for the manager UI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time

import streamlit as st

from schedule_adjustment_tool.domain.models import Config, Participant
from schedule_adjustment_tool.ui.manager.app_cache import (
    render_project_operation_feedback,
)


@dataclass(frozen=True)
class ProjectSetupServices:
    """Entry-point coordination required by project setup forms."""

    max_text_length: int
    max_description_length: int
    status_labels: Mapping[str, str]
    weekday_labels: Mapping[int, str]
    basic_settings_locked: Callable[[Config], bool]
    project_config_draft: Callable[[str, Config], dict]
    save_project_settings: Callable[..., None]
    confirm_response_reopen: Callable[..., None]


def render_project_basic_settings(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None = None,
    *,
    services: ProjectSetupServices,
) -> None:
    # Basic project information remains editable while responses are being
    # collected. This does not alter the response window or submitted data.
    settings_locked = False
    input_config = Config.from_dict(
        services.project_config_draft(project_id, config)
    )
    performance_date = (
        date.fromisoformat(input_config.performance_date)
        if input_config.performance_date
        else date.fromisoformat(input_config.end_date)
    )
    title = st.text_input(
        "企画名",
        value=input_config.title,
        max_chars=services.max_text_length,
        disabled=settings_locked,
        key=f"focused_project_title_{project_id}",
    )
    description = st.text_area(
        "説明・連絡事項",
        value=input_config.description,
        height=120,
        max_chars=services.max_description_length,
        key=f"focused_project_description_{project_id}",
    )
    use_performance_date = st.toggle(
        "本番日を設定する",
        value=bool(input_config.performance_date),
        disabled=settings_locked,
        key=f"focused_project_use_performance_date_{project_id}",
    )
    selected_performance_date = st.date_input(
        "本番日",
        value=performance_date,
        help="本番直前の開催を避ける評価の基準日です。",
        disabled=settings_locked or not use_performance_date,
        key=f"focused_project_performance_date_{project_id}",
    )
    save_clicked = st.button(
        "基本情報を保存",
        type="primary",
        key=f"focused_project_basic_save_{project_id}",
    )
    render_project_operation_feedback(project_id, "project_basic")
    if not save_clicked:
        return
    if not title.strip():
        st.warning("企画名を入力してください。")
        return
    updates: dict[str, object] = {"description": description.strip()}
    updates.update(
        {
            "title": title.strip(),
            "performance_date": (
                selected_performance_date.isoformat()
                if use_performance_date
                else ""
            ),
        }
    )
    services.save_project_settings(
        project_id,
        config,
        participants,
        updates,
        success_message="基本情報を保存しました。",
        workflow_step_id="project_setup",
        confirmed=confirmed,
        published_conflict=True,
        feedback_operation_key="project_basic",
    )


def render_response_window_settings(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None = None,
    *,
    services: ProjectSetupServices,
) -> None:
    reopen_notice_key = f"response_window_reopen_notice_{project_id}"
    if st.session_state.pop(reopen_notice_key, False):
        st.warning(
            "回答受付を再開する代わりに、締切後も参加者による編集を許可"
            "する設定をONにすることを推奨します。"
        )
    settings_locked = services.basic_settings_locked(config)
    input_config = (
        config
        if settings_locked
        else Config.from_dict(services.project_config_draft(project_id, config))
    )
    if settings_locked:
        st.info(
            "回答受付中は期間・曜日・時限を変更できません。"
            "締切後の編集許可は変更できます。"
        )
    deadline_value = (
        datetime.fromisoformat(input_config.response_deadline)
        if input_config.response_deadline
        else datetime.combine(
            date.fromisoformat(input_config.end_date),
            time(23, 59),
        )
    )
    with st.form(f"focused_response_window_{project_id}"):
        status = st.selectbox(
            "企画状態",
            list(services.status_labels),
            index=list(services.status_labels).index(input_config.status),
            format_func=lambda value: services.status_labels[value],
            help=(
                "準備ができたら「回答受付中」に変更して保存します。"
                "回答を締め切る場合は「回答締切」に変更します。"
            ),
        )
        date_columns = st.columns(4)
        start_date = date_columns[0].date_input(
            "日調開始日",
            value=date.fromisoformat(input_config.start_date),
            disabled=settings_locked,
        )
        end_date = date_columns[1].date_input(
            "日調終了日",
            value=date.fromisoformat(input_config.end_date),
            disabled=settings_locked,
        )
        deadline_date = date_columns[2].date_input(
            "入力締切日",
            value=deadline_value.date(),
            disabled=settings_locked,
        )
        deadline_time = date_columns[3].time_input(
            "入力締切時刻",
            value=deadline_value.time(),
            disabled=settings_locked,
        )
        target_columns = st.columns(2)
        enabled_weekdays = target_columns[0].multiselect(
            "日調対象曜日",
            list(services.weekday_labels),
            default=input_config.enabled_weekdays,
            format_func=lambda value: services.weekday_labels[value],
            disabled=settings_locked,
        )
        enabled_periods = target_columns[1].multiselect(
            "日調対象時限",
            list(range(1, 7)),
            default=input_config.enabled_periods,
            format_func=lambda value: f"{value}限",
            disabled=settings_locked,
        )
        allow_edits_after_deadline = st.checkbox(
            "締切後も参加者による編集を許可",
            value=input_config.allow_edits_after_deadline,
        )
        save_clicked = st.form_submit_button(
            "回答受付設定を保存",
            type="primary",
        )
    render_project_operation_feedback(project_id, "response_window")
    if not save_clicked:
        return
    updates: dict[str, object] = {
        "status": status,
        "allow_edits_after_deadline": allow_edits_after_deadline,
    }
    if not settings_locked:
        updates.update(
            {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "response_deadline": datetime.combine(
                    deadline_date,
                    deadline_time,
                ).isoformat(timespec="minutes"),
                "enabled_weekdays": sorted(enabled_weekdays),
                "enabled_periods": sorted(enabled_periods),
            }
        )
    if config.status == "closed" and status == "collecting":
        services.confirm_response_reopen(
            project_id,
            config.to_dict(),
            [participant.to_dict() for participant in participants],
            updates,
            confirmed,
        )
        return
    services.save_project_settings(
        project_id,
        config,
        participants,
        updates,
        success_message="回答受付設定を保存しました。",
        confirmed=confirmed,
        published_conflict=True,
        feedback_operation_key="response_window",
    )
