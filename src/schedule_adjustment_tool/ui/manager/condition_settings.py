"""Manager screens for candidate constraints and evaluation preferences."""

from __future__ import annotations

import html
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.domain.evaluation_config import (
    EVALUATION_DEFINITIONS,
    EVALUATION_DISPLAY_ORDER,
    PRIORITY_LABELS,
    PRIORITY_LEVELS,
    normalize_evaluation_settings,
)
from schedule_adjustment_tool.domain.models import (
    Config,
    GROUP_FIELD_OPTIONS,
    PARTICIPATION_MODE_ROLE_BASED,
    PARTICIPATION_MODE_TOTAL_ONCE,
    Participant,
    WEEKDAY_LABELS,
)
from schedule_adjustment_tool.ui.manager.app_cache import (
    render_project_operation_feedback,
    status_message,
)
from schedule_adjustment_tool.ui.design_tokens import ZOOM_BACKGROUND, ZOOM_BORDER


EVALUATION_DISABLED_POLICY = "__evaluation_disabled__"
EVALUATION_DISABLED_LABEL = "評価対象外"


@dataclass(frozen=True)
class ConditionSettingsServices:
    """App-level saves, dialogs, and workflow coordination for these screens."""

    basic_project_settings_locked: Callable[[Config], bool]
    project_config_draft: Callable[[str, Config], dict]
    update_project_config_draft: Callable[[str, dict], None]
    save_project_settings: Callable[..., None]
    changed_config_updates: Callable[[Config, dict], dict]
    update_config_and_clear_candidates: Callable[[str, Config, dict], None]
    candidate_affecting_config_keys: Callable[[], set[str]]
    confirm_global_condition_change: Callable[..., None]
    confirm_published_schedule_change: Callable[..., None]
    mark_step_started: Callable[[str, str], None]
    show_note: Callable[[str], None]
    section_heading: Callable[..., None]


def render_evaluation_settings(
    project_id: str,
    config: Config,
) -> tuple[dict[str, dict[str, object]], bool]:
    """Render the single 100-point candidate-evaluation configuration form."""

    draft_key = f"evaluation_settings_draft_{project_id}"
    if draft_key not in st.session_state:
        st.session_state[draft_key] = normalize_evaluation_settings(
            config.evaluation_settings
        )
    draft = st.session_state[draft_key]

    _render_section_heading(
        "候補評価設定",
        "各項目について、方針と優先度を設定します。",
        level=5,
    )
    st.caption(
        "総合適合度は100点満点で、100が最も設定した方針に合う候補です。"
        "最優先:優先:考慮=4:2:1の割合で総合適合度に反映されます。"
    )
    with st.form(
        f"evaluation_detail_settings_form_{project_id}",
        enter_to_submit=False,
    ):
        for evaluation_id in EVALUATION_DISPLAY_ORDER:
            definition = EVALUATION_DEFINITIONS[evaluation_id]
            columns = st.columns([2.8, 2.8, 1.2], gap="small")
            columns[0].markdown(
                f"<div class='evaluation-label'>{html.escape(definition['label'])}</div>",
                unsafe_allow_html=True,
            )
            policies = definition["policies"]
            policy_values = [
                policy_id
                for policy_id in policies
                if policy_id != "ignore"
            ]
            policy_options = [EVALUATION_DISABLED_POLICY, *policy_values]
            current_policy = str(draft[evaluation_id]["policy"])
            selected_policy = (
                EVALUATION_DISABLED_POLICY
                if (
                    not bool(draft[evaluation_id]["enabled"])
                    or current_policy == "ignore"
                )
                else current_policy
            )
            policy = columns[1].selectbox(
                "方針",
                policy_options,
                index=(
                    policy_options.index(selected_policy)
                    if selected_policy in policy_options
                    else 0
                ),
                format_func=lambda value, labels=policies: (
                    EVALUATION_DISABLED_LABEL
                    if value == EVALUATION_DISABLED_POLICY
                    else labels[value]
                ),
                help=str(definition["description"]),
                key=f"evaluation_policy_{project_id}_{evaluation_id}",
            )
            current_priority = str(draft[evaluation_id]["priority"])
            priority = columns[2].selectbox(
                "優先度",
                PRIORITY_LEVELS,
                index=(
                    PRIORITY_LEVELS.index(current_priority)
                    if current_priority in PRIORITY_LEVELS
                    else 0
                ),
                format_func=lambda value: PRIORITY_LABELS[value],
                key=f"evaluation_priority_{project_id}_{evaluation_id}",
            )
            draft[evaluation_id]["enabled"] = policy != EVALUATION_DISABLED_POLICY
            draft[evaluation_id]["policy"] = (
                policy
                if policy != EVALUATION_DISABLED_POLICY
                else (
                    current_policy
                    if current_policy in policy_values
                    else policy_values[0]
                )
            )
            draft[evaluation_id]["priority"] = priority
        save_clicked = st.form_submit_button(
            "評価項目・優先度を保存",
            type="primary",
        )
    st.session_state[draft_key] = draft
    return normalize_evaluation_settings(draft), save_clicked


@st.fragment
def _render_group_settings_fragment(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None = None,
    *,
    services: ConditionSettingsServices,
) -> None:
    """Render the group-count editor in an isolated Streamlit fragment."""

    # Group structure can be adjusted during response collection. The change
    # is applied to scheduling conditions, not to participants' responses.
    settings_locked = False
    input_config = Config.from_dict(
        services.project_config_draft(project_id, config)
    )
    group_count = st.number_input(
        "班の数",
        min_value=1,
        max_value=100,
        value=input_config.group_count,
        disabled=settings_locked,
        key=f"focused_group_count_{project_id}",
    )
    with st.form(f"focused_group_settings_{project_id}"):
        assignment_frame = pd.DataFrame(
            [
                {
                    "班": group_number,
                    "対応文理": input_config.group_field_assignments.get(
                        str(group_number),
                        "文理混合",
                    ),
                }
                for group_number in range(1, int(group_count) + 1)
            ]
        )
        edited_assignments = st.data_editor(
            assignment_frame,
            hide_index=True,
            width="stretch",
            height=min(520, 36 * (len(assignment_frame) + 1)),
            disabled=True if settings_locked else ["班"],
            column_config={
                "班": st.column_config.NumberColumn("班", width="small"),
                "対応文理": st.column_config.SelectboxColumn(
                    "当日対応予定の高校生",
                    options=GROUP_FIELD_OPTIONS,
                    required=True,
                ),
            },
            key=f"focused_group_assignments_{project_id}_{int(group_count)}",
        )
        save_clicked = st.form_submit_button(
            "班構成を保存",
            type="primary",
            disabled=settings_locked,
        )
    render_project_operation_feedback(project_id, "group_settings")
    if not save_clicked:
        return
    group_field_assignments = {
        str(int(row["班"])): str(row["対応文理"])
        for _, row in edited_assignments.sort_values("班").iterrows()
    }
    services.save_project_settings(
        project_id,
        config,
        participants,
        {
            "group_count": int(group_count),
            "group_field_assignments": group_field_assignments,
        },
        success_message="班構成を保存しました。",
        workflow_step_id="participants",
        confirmed=confirmed,
        published_conflict=True,
        feedback_operation_key="group_settings",
    )


def render_group_settings(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None = None,
    *,
    services: ConditionSettingsServices,
) -> None:
    """Render group count and field assignments."""

    _render_group_settings_fragment(
        project_id,
        config,
        participants,
        confirmed,
        services=services,
    )


def render_role_and_participation_settings(
    project_id: str,
    config: Config,
    participants: list[Participant] | None = None,
    confirmed: dict | None = None,
    *,
    services: ConditionSettingsServices,
) -> None:
    """Render candidate-generation role and attendance constraints."""

    # Candidate constraints can change during response collection; they do not
    # overwrite a participant's submitted availability.
    settings_locked = False
    input_config = Config.from_dict(services.project_config_draft(project_id, config))
    services.section_heading(
        "役割・参加条件",
        "ここで設定する内容は候補生成の必須条件です。"
        "役割指定なしの参加者は、大学生役・高校生役を問わずに練習会へ参加します。",
    )
    participation_requirement_mode = st.radio(
        "参加回数ルール",
        [PARTICIPATION_MODE_TOTAL_ONCE, PARTICIPATION_MODE_ROLE_BASED],
        index=(
            0
            if input_config.participation_requirement_mode
            == PARTICIPATION_MODE_TOTAL_ONCE
            else 1
        ),
        format_func=lambda value: (
            "合計回数のみ指定"
            if value == PARTICIPATION_MODE_TOTAL_ONCE
            else "大学生役・高校生役ごとに回数を指定"
        ),
        horizontal=True,
        help=(
            "ここで設定する内容は候補生成の必須条件です。"
            "役割指定なしの参加者は、個人別の練習会参加数に従います。"
        ),
        key=f"participation_requirement_mode_{project_id}",
    )
    with st.form(f"role_participation_settings_{project_id}"):
        role_columns = st.columns(2)
        university_role_size = role_columns[0].number_input(
            "1組の大学生役人数",
            1,
            20,
            input_config.university_role_size,
            disabled=settings_locked,
            key=f"university_role_size_{project_id}",
        )
        high_school_role_size = role_columns[1].number_input(
            "1組の高校生役人数",
            1,
            20,
            input_config.high_school_role_size,
            disabled=settings_locked,
            key=f"high_school_role_size_{project_id}",
        )
        required_university_count = input_config.required_university_count
        required_high_school_count = input_config.required_high_school_count
        required_total_count = input_config.required_total_count
        total_extra_limit = input_config.total_extra_limit
        if participation_requirement_mode == PARTICIPATION_MODE_ROLE_BASED:
            requirement_columns = st.columns(3)
            required_university_count = requirement_columns[0].number_input(
                "大学生役の規定回数",
                0,
                20,
                input_config.required_university_count,
                disabled=settings_locked,
                key=f"required_university_count_{project_id}",
            )
            required_high_school_count = requirement_columns[1].number_input(
                "高校生役の規定回数",
                0,
                20,
                input_config.required_high_school_count,
                disabled=settings_locked,
                key=f"required_high_school_count_{project_id}",
            )
        else:
            required_total_count = st.number_input(
                "合計の練習会参加数",
                0,
                20,
                input_config.required_total_count,
                disabled=settings_locked,
                help=(
                    "保存すると、全参加者の成立条件を役割指定なしにし、"
                    "この回数を個人別の練習会参加数へ設定します。"
                ),
                key=f"required_total_count_{project_id}",
            )
        total_extra_limit = st.number_input(
            "合計参加の超過上限",
            0,
            20,
            input_config.total_extra_limit,
            disabled=settings_locked,
            help=(
                "規定回数を超えて割り当ててもよい最大回数です。"
                "探索では実際の超過をまず0に近づけ、"
                "必要な場合だけこの上限まで使用します。"
            ),
            key=f"total_extra_limit_{project_id}",
        )
        limit_columns = st.columns(3)
        max_groups_per_slot = limit_columns[0].number_input(
            "1コマ最大組数",
            1,
            10,
            input_config.max_groups_per_slot,
            disabled=settings_locked,
            help="同じ日付・時限に同時開催できる組数です。",
            key=f"max_groups_per_slot_{project_id}",
        )
        max_sessions_per_day = limit_columns[1].number_input(
            "1人あたり1日上限",
            1,
            20,
            input_config.max_sessions_per_person_per_day,
            disabled=settings_locked,
            help="同じ参加者が1日に参加できる最大回数です。",
            key=f"max_sessions_per_day_{project_id}",
        )
        avoid_consecutive_periods = limit_columns[2].checkbox(
            "連続コマを禁止",
            value=input_config.avoid_consecutive_periods,
            disabled=settings_locked,
            help="ONにすると、同じ参加者が連続する時限に割り当てられないようにします。",
            key=f"avoid_consecutive_periods_{project_id}",
        )
        save_clicked = st.form_submit_button(
            "役割・参加条件を保存",
            type="primary",
            disabled=settings_locked,
        )
    render_project_operation_feedback(project_id, "role_conditions")
    updates = {
        "participation_requirement_mode": participation_requirement_mode,
        "required_total_count": int(required_total_count),
        "university_role_size": int(university_role_size),
        "high_school_role_size": int(high_school_role_size),
        "required_university_count": int(required_university_count),
        "required_high_school_count": int(required_high_school_count),
        "total_extra_limit": int(total_extra_limit),
        "max_groups_per_slot": int(max_groups_per_slot),
        "max_sessions_per_person_per_day": int(max_sessions_per_day),
        "avoid_consecutive_periods": avoid_consecutive_periods,
    }
    if save_clicked:
        changed = services.changed_config_updates(config, updates)
        individual_default_keys = {
            "participation_requirement_mode",
            "required_total_count",
            "required_university_count",
            "required_high_school_count",
            "total_extra_limit",
        }
        if changed and participants and individual_default_keys & set(changed):
            services.confirm_global_condition_change(
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
            participants or [],
            updates,
            success_message="役割・参加条件を保存しました。",
            workflow_step_id="conditions",
            confirmed=confirmed,
            published_conflict=True,
            feedback_operation_key="role_conditions",
        )
        return

    render_excluded_date_settings(
        project_id,
        config,
        participants or [],
        confirmed,
        services=services,
    )


def _excluded_date_range(input_config: Config) -> list[date]:
    try:
        start = date.fromisoformat(input_config.start_date)
        end = date.fromisoformat(input_config.end_date)
    except ValueError:
        return []
    if start > end:
        return []
    return [
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
    ]


def _shift_month(value: date, offset: int) -> date:
    month_index = value.year * 12 + value.month - 1 + offset
    year, month_index = divmod(month_index, 12)
    return date(year, month_index + 1, 1)


def _set_excluded_date_calendar_month(month_key: str, value: date) -> None:
    st.session_state[month_key] = value.isoformat()


def _toggle_excluded_date(draft_key: str, day_text: str) -> None:
    excluded_dates = {
        str(value) for value in st.session_state.get(draft_key, set())
    }
    if day_text in excluded_dates:
        excluded_dates.discard(day_text)
    else:
        excluded_dates.add(day_text)
    st.session_state[draft_key] = excluded_dates


@st.fragment
def render_excluded_date_settings(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None = None,
    *,
    services: ConditionSettingsServices,
) -> None:
    """Render the independent practice-date exclusion calendar."""

    st.divider()
    st.subheader("練習会の除外日")
    st.caption(
        "新しい候補や手動調整で練習会を置かない日を指定します。"
        "既存の回答・候補・確定日程は変更しません。"
    )
    input_config = Config.from_dict(
        services.project_config_draft(project_id, config)
    )
    days = _excluded_date_range(input_config)
    if not days:
        st.info("除外日を設定できる期間がありません。")
        return

    draft_key = f"excluded_dates_draft_{project_id}"
    if draft_key not in st.session_state:
        st.session_state[draft_key] = set(input_config.excluded_dates)
    excluded_dates = {
        str(value) for value in st.session_state[draft_key]
    }
    first_month = days[0].replace(day=1)
    last_month = days[-1].replace(day=1)
    month_key = f"excluded_dates_month_{project_id}"
    raw_month = st.session_state.get(month_key, first_month.isoformat())
    try:
        selected_month = date.fromisoformat(str(raw_month)).replace(day=1)
    except ValueError:
        selected_month = first_month
    selected_month = min(last_month, max(first_month, selected_month))
    st.session_state[month_key] = selected_month.isoformat()

    with st.container(width=520):
        navigation = st.columns([1, 2, 1])
        previous_month = _shift_month(selected_month, -1)
        next_month = _shift_month(selected_month, 1)
        navigation[0].button(
            "‹ 前月",
            disabled=selected_month <= first_month,
            key=f"excluded_dates_previous_{project_id}",
            width="stretch",
            on_click=_set_excluded_date_calendar_month,
            args=(month_key, previous_month),
        )
        navigation[1].markdown(
            f"<div style='text-align:center; padding:0.45rem 0; "
            f"font-weight:650;'>{selected_month.year}年"
            f"{selected_month.month}月</div>",
            unsafe_allow_html=True,
        )
        navigation[2].button(
            "次月 ›",
            disabled=selected_month >= last_month,
            key=f"excluded_dates_next_{project_id}",
            width="stretch",
            on_click=_set_excluded_date_calendar_month,
            args=(month_key, next_month),
        )

        st.markdown(
            f"""
<style>
div[class*="st-key-excluded_date_"] button {{
    border: 1px solid #d0d5dd !important;
    color: #667085 !important;
    background: #ffffff !important;
}}
div[class*="st-key-available_date_"] button {{
    border: 1px solid #d0d5dd !important;
    color: #175cd3 !important;
    background: {ZOOM_BACKGROUND} !important;
    border-color: {ZOOM_BORDER} !important;
}}
.excluded-date-weekday {{
    text-align: center;
    color: #667085;
    font-size: 0.875rem;
    padding: 0.25rem 0;
}}
.excluded-date-disabled {{
    text-align: center;
    color: #98a2b3;
    padding: 0.55rem 0;
}}
</style>
            """,
            unsafe_allow_html=True,
        )
        weekday_order = (6, 0, 1, 2, 3, 4, 5)
        header_columns = st.columns(7)
        for column, weekday in zip(
            header_columns,
            weekday_order,
            strict=True,
        ):
            column.markdown(
                f"<div class='excluded-date-weekday'>"
                f"{WEEKDAY_LABELS[weekday]}</div>",
                unsafe_allow_html=True,
            )

        month_end = _shift_month(selected_month, 1) - timedelta(days=1)
        month_days = [
            selected_month + timedelta(days=offset)
            for offset in range((month_end - selected_month).days + 1)
        ]
        grid_cells: list[date | None] = [
            *([None] * ((selected_month.weekday() + 1) % 7)),
            *month_days,
        ]
        while len(grid_cells) % 7:
            grid_cells.append(None)
        editable_days = {
            day
            for day in days
            if day.weekday() in set(input_config.enabled_weekdays)
        }
        for row_start in range(0, len(grid_cells), 7):
            columns = st.columns(7)
            for column, day in zip(
                columns,
                grid_cells[row_start : row_start + 7],
                strict=True,
            ):
                if day is None:
                    column.empty()
                    continue
                day_text = day.isoformat()
                is_excluded = day_text in excluded_dates
                in_range = day in editable_days
                if not in_range:
                    column.markdown(
                        f"<div class='excluded-date-disabled'>{day.day}</div>",
                        unsafe_allow_html=True,
                    )
                    continue
                column.button(
                    str(day.day),
                    type="tertiary" if is_excluded else "secondary",
                    help=(
                        f"{day_text}の除外を解除"
                        if is_excluded
                        else f"{day_text}を除外"
                    ),
                    key=(
                        f"excluded_date_{project_id}_{day_text}"
                        if is_excluded
                        else f"available_date_{project_id}_{day_text}"
                    ),
                    width="stretch",
                    on_click=_toggle_excluded_date,
                    args=(draft_key, day_text),
                )

        st.caption(
            "背景色のマスが対象期間内かつ日調対象曜日の設定可能日、"
            "白い枠のマスが除外日です。枠のない日付は変更できません。"
            "除外日を設定しても、既存の候補・確定日程は自動削除されません。"
        )
        save_excluded_dates_clicked = st.button(
            "除外日を保存",
            type="primary",
            key=f"save_excluded_dates_{project_id}",
        )
        render_project_operation_feedback(project_id, "excluded_dates")
        if save_excluded_dates_clicked:
            services.save_project_settings(
                project_id,
                config,
                participants,
                {"excluded_dates": sorted(excluded_dates)},
                success_message="練習会の除外日を保存しました。",
                workflow_step_id="conditions",
                confirmed=confirmed,
                published_conflict=True,
                feedback_operation_key="excluded_dates",
            )


def render_evaluation_preferences_settings(
    project_id: str,
    config: Config,
    participants: list[Participant] | None = None,
    confirmed: dict | None = None,
    *,
    services: ConditionSettingsServices,
) -> None:
    """Render evaluation policies and their date/period preference controls."""

    evaluation_feedback_key = f"evaluation_settings_feedback_{project_id}"
    period_feedback_key = (
        f"evaluation_period_settings_feedback_{project_id}"
    )

    evaluation_settings, save_evaluation_clicked = render_evaluation_settings(
        project_id,
        config,
    )
    _render_evaluation_feedback(evaluation_feedback_key)
    if save_evaluation_clicked:
        updates = {"evaluation_settings": evaluation_settings}
        changed = services.changed_config_updates(config, updates)
        if changed and confirmed:
            services.confirm_published_schedule_change(
                project_id,
                config.to_dict(),
                [participant.to_dict() for participant in (participants or [])],
                updates,
                "評価項目と優先度を保存しました。",
                "conditions",
                feedback_session_key=evaluation_feedback_key,
                cancel_reset_keys=(
                    f"evaluation_settings_draft_{project_id}",
                    *(
                        f"evaluation_policy_{project_id}_{evaluation_id}"
                        for evaluation_id in EVALUATION_DISPLAY_ORDER
                    ),
                    *(
                        f"evaluation_priority_{project_id}_{evaluation_id}"
                        for evaluation_id in EVALUATION_DISPLAY_ORDER
                    ),
                ),
            )
            return
        with status_message("評価項目・優先度を保存しています..."):
            services.update_config_and_clear_candidates(
                project_id,
                config,
                updates,
            )
            st.session_state[f"evaluation_settings_draft_{project_id}"] = (
                normalize_evaluation_settings(evaluation_settings)
            )
        feedback_kind, feedback_message = _candidate_reset_feedback(
            "評価項目と優先度を保存しました。",
            changed,
            services,
        )
        st.session_state[evaluation_feedback_key] = {
            "kind": feedback_kind,
            "message": feedback_message,
        }
        if changed:
            services.mark_step_started(project_id, "conditions")
        st.rerun()

    st.divider()
    services.section_heading(
        "評価に使う期間・時限",
        "本番直前の回避日数は、本番日より前の指定日数内にある練習会を"
        "低く評価します。避ける時限も禁止ではなくマイナス評価です。",
    )
    performance_enabled = bool(evaluation_settings["performance_buffer"]["enabled"])
    option_columns = st.columns(2)
    performance_avoid_days = int(
        option_columns[0].number_input(
            "本番直前の回避日数",
            0,
            60,
            config.performance_avoid_days,
            help=(
                "本番日より前の指定日数内にある練習会を低く評価します。"
                "禁止ではなくマイナス評価です。"
            ),
            key=f"performance_avoid_days_{project_id}",
        )
    )
    avoided_periods = option_columns[1].multiselect(
        "避ける時限",
        list(range(1, 7)),
        default=config.avoided_periods,
        format_func=lambda period: f"{period}限",
        help="指定した時限に練習会を置く候補を低く評価します。禁止ではありません。",
        key=f"avoided_periods_{project_id}",
    )
    if performance_enabled and not config.performance_date:
        st.warning("本番日が未設定です。企画情報の設定で設定してください。")

    save_period_clicked = st.button(
        "期間・時限の評価設定を保存",
        type="primary",
        key=f"save_evaluation_period_settings_{project_id}",
    )
    if not save_period_clicked:
        _render_evaluation_feedback(period_feedback_key)
        return
    updates = {
        "performance_avoid_days": performance_avoid_days,
        "avoided_periods": sorted(avoided_periods),
    }
    changed = services.changed_config_updates(config, updates)
    if changed and confirmed:
        services.confirm_published_schedule_change(
            project_id,
            config.to_dict(),
            [participant.to_dict() for participant in (participants or [])],
            updates,
            "期間・時限の評価設定を保存しました。",
            "conditions",
            feedback_session_key=period_feedback_key,
        )
        return
    with status_message("期間・時限の評価設定を保存しています..."):
        services.update_config_and_clear_candidates(project_id, config, updates)
    feedback_kind, feedback_message = _candidate_reset_feedback(
        "期間・時限の評価設定を保存しました。",
        changed,
        services,
    )
    st.session_state[period_feedback_key] = {
        "kind": feedback_kind,
        "message": feedback_message,
    }
    if changed:
        services.mark_step_started(project_id, "conditions")
    st.rerun()


def _render_evaluation_feedback(session_key: str) -> None:
    feedback = st.session_state.pop(session_key, None)
    if not isinstance(feedback, dict):
        return
    if feedback.get("kind") == "info":
        st.info(str(feedback.get("message", "")))
    else:
        st.success(str(feedback.get("message", "")))


def _render_section_heading(title: str, help_text: str, *, level: int) -> None:
    safe_level = min(6, max(3, level))
    st.markdown(
        f"<div class='section-heading'><h{safe_level}>{html.escape(title)}</h{safe_level}></div>",
        unsafe_allow_html=True,
    )
    st.caption(help_text)


def _candidate_reset_feedback(
    success_message: str,
    changed: dict,
    services: ConditionSettingsServices,
) -> tuple[str, str]:
    if services.candidate_affecting_config_keys() & set(changed):
        return "success", f"{success_message} 保存済み候補を整理しました。"
    elif changed:
        return "success", success_message
    return "info", "保存する変更はありません。"
