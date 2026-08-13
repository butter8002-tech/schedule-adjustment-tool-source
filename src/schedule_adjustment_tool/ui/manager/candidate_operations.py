"""Candidate detail and calendar-based adjustment UI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time as time_module

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.domain.evaluation_config import (
    EVALUATION_DEFINITIONS,
    EVALUATION_DISPLAY_ORDER,
    EVALUATION_SCORE_VERSION,
    PRIORITY_LABELS,
    PRIORITY_LEVELS,
    normalize_evaluation_settings,
)
from schedule_adjustment_tool.domain.models import (
    Config,
    Participant,
    ROLE_DISPLAY_COLORS,
    ROLE_DISPLAY_LABELS,
    now_iso,
    practice_dates,
)
from schedule_adjustment_tool.exports.spreadsheet_exports import candidate_workbook
from schedule_adjustment_tool.storage import confirm_candidate
from schedule_adjustment_tool.domain.schedule_model import (
    ScheduleModelError,
    schedule_policy_issues,
)
from schedule_adjustment_tool.ui.calendar_views import (
    calendar_table_html,
    candidate_calendar_frames,
    candidate_full_calendar,
    role_display_legend_html,
)
from schedule_adjustment_tool.ui.manager.app_cache import (
    export_cache_token,
    status_message,
)
from schedule_adjustment_tool.ui.manager.candidate_evaluation import (
    build_evaluation_profiles,
    profile_label_for_candidate,
)
from schedule_adjustment_tool.ui.manager.candidate_calendar import (
    assignment_locks_from_calendar_sessions,
    calendar_dates_for_editing,
    calendar_participation_limit,
    calendar_required_role_counts,
    candidate_from_calendar_sessions,
)
from schedule_adjustment_tool.ui.manager.project_cache import (
    set_cached_confirmed,
    update_cached_config,
)
from schedule_adjustment_tool.ui.manager.session_state import (
    mark_manager_step_completed,
)
from schedule_adjustment_tool.ui.formatting import format_date_with_weekday
from schedule_adjustment_tool.ui.design_tokens import (
    HIGH_SCHOOL_ROLE,
    UNIVERSITY_ROLE,
)
from schedule_adjustment_tool.ui.presentation import ROLE_DISPLAY_MODE_CHOICES


ROLE_DISPLAY_LABEL_OPTION, ROLE_DISPLAY_COLOR_OPTION = ROLE_DISPLAY_MODE_CHOICES


def _render_candidate_operation_feedback(editor_key: str) -> None:
    feedback = st.session_state.pop(f"{editor_key}_feedback", None)
    if not isinstance(feedback, dict):
        return
    if feedback.get("success"):
        st.success(str(feedback["success"]))
    for warning in feedback.get("warnings", []):
        st.warning(str(warning))
    if feedback.get("info"):
        st.info(str(feedback["info"]))


def _candidate_has_evaluation_config(candidate: dict) -> bool:
    snapshot = candidate.get("evaluation_config")
    return isinstance(snapshot, dict) and isinstance(
        snapshot.get("evaluation_settings"),
        dict,
    )


@dataclass(frozen=True)
class CandidateOperationServices:
    """Application operations invoked from the candidate interaction UI."""

    max_stored_candidates: int
    schedule_calendar_editor: Callable[..., list[dict]]
    render_prepared_download: Callable[..., None]
    append_project_candidates: Callable[..., tuple[list[dict], list[dict], int, int]]
    generate_candidates_with_assignment_locks: Callable[
        ..., tuple[list[dict], list[str], str]
    ]


def _render_calendar_table(
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


def candidate_publication_warnings(
    config: Config,
    candidate: dict,
    participants: list[Participant],
) -> tuple[str, ...]:
    """Return warnings that require explicit acknowledgement before publish.

    A response is counted only when the participant is both active and
    approved, matching the manager summary and candidate-generation target.
    Participants outside the scheduling target therefore never create an
    unsubmitted-response warning.
    """

    target = [
        participant
        for participant in participants
        if participant.active and participant.approved
    ]
    unsubmitted_count = sum(
        participant.input_status != "submitted" for participant in target
    )
    warnings: list[str] = []
    if unsubmitted_count:
        warnings.append(
            f"日調対象の未提出者が{unsubmitted_count}人います。"
        )
    if config.status not in {"closed", "confirmed"}:
        warnings.append(
            "回答受付はまだ締め切られていません。"
            "この候補を先行作業として公開します。"
        )
    if not candidate.get("metrics", {}).get("is_strict_candidate", True):
        warnings.append(
            "この候補は必須条件を満たしていない項目があります。"
        )
    try:
        warnings.extend(schedule_policy_issues(candidate, config, participants))
    except ScheduleModelError as error:
        warnings.append(f"候補の日程データを確認できません: {error}")
    return tuple(dict.fromkeys(warnings))


def _candidate_display_data(
    project_id: str,
    config: Config,
    candidate: dict,
    *,
    candidate_version: int | None,
    evaluation_settings: dict,
) -> dict[str, object]:
    """Cache pure candidate tables by the saved data versions."""

    cache = st.session_state.setdefault("candidate_display_data_cache", {})
    cache_key = export_cache_token(
        {
            "project_id": project_id,
            "config_version": config._storage_version,
            "config": config.to_dict(),
            "candidate_version": candidate_version,
            "candidate": candidate,
            "evaluation_settings": evaluation_settings,
        }
    )
    if cache_key in cache:
        return cache[cache_key]

    metrics = candidate.get("metrics", {})
    penalties = metrics.get("evaluation_penalties", {})
    priority_penalties = metrics.get("evaluation_priority_penalties", {}) or {}
    priority_item_counts = metrics.get(
        "evaluation_priority_item_counts", {}
    ) or {}

    def evaluation_fit_score(evaluation_id: str) -> float:
        try:
            penalty = float(penalties.get(evaluation_id, 0))
        except (TypeError, ValueError):
            penalty = 0.0
        return max(0.0, min(100.0, 100.0 - penalty))

    del priority_penalties, priority_item_counts
    evidence_by_id = {
        "performance_buffer": (
            f"本番直前の開催 {metrics.get('performance_buffer_session_count', 0)}回"
        ),
        "avoid_periods": (
            f"避ける時限の開催 {metrics.get('avoided_period_session_count', 0)}回"
        ),
        "zoom_meeting": (
            f"Zoom開催 {metrics.get('zoom_session_count', 0)}回、"
            f"必要なZoom {metrics.get('necessary_zoom_session_count', 0)}回"
        ),
        "cohort_balance": (
            f"最新期のみの組 {metrics.get('cohort_latest_only_session_count', 0)}組"
        ),
        "same_group": (
            f"同班編成 {metrics.get('university_group_match_count', 0)} / "
            f"{metrics.get('university_group_evaluated_count', 0)}回適合"
        ),
        "field_match": (
            f"文理対応 {metrics.get('field_match_count', 0)} / "
            f"{metrics.get('field_evaluated_count', 0)}回適合"
        ),
        "session_count": (
            f"開催組数 {metrics.get('number_of_sessions', len(candidate.get('sessions', [])))}組"
        ),
        "participant_schedule": "同日連続または別日分散の配置を評価",
        "overall_schedule": "開催日を期間内に分散または集約",
    }
    evaluation_rows: list[dict[str, object]] = []
    for priority in PRIORITY_LEVELS:
        for evaluation_id in EVALUATION_DISPLAY_ORDER:
            setting = evaluation_settings[evaluation_id]
            if not setting["enabled"] or setting["priority"] != priority:
                continue
            definition = EVALUATION_DEFINITIONS[evaluation_id]
            evaluation_rows.append(
                {
                    "優先度": PRIORITY_LABELS[priority],
                    "評価項目": definition["label"],
                    "設定方針": definition["policies"].get(
                        setting["policy"], setting["policy"]
                    ),
                    "適合度（100が最良）": round(
                        evaluation_fit_score(evaluation_id),
                        1,
                    ),
                    "この数字の根拠": evidence_by_id.get(evaluation_id, ""),
                }
            )

    slot_counts: dict[tuple[str, int], int] = {}
    for session in candidate.get("sessions", []):
        slot_key = (session["date"], int(session["period"]))
        slot_counts[slot_key] = slot_counts.get(slot_key, 0) + 1
    schedule_rows = [
        {
            "回": session_index,
            "日時": (
                f"{format_date_with_weekday(session['date'])} "
                f"{session['period']}限"
            ),
            "開催形式": (
                "Zoom" if session.get("meeting_mode") == "zoom" else "対面"
            ),
            "組": (
                f"組{session['group_index']}"
                if slot_counts[(session["date"], int(session["period"]))] > 1
                else ""
            ),
            "大学生役": "、".join(session["university_role_members"]),
            "高校生役": "、".join(session["high_school_role_members"]),
            "大学生役人数不足": int(
                session.get("university_role_shortfall", 0)
            ),
            "高校生役人数不足": int(
                session.get("high_school_role_shortfall", 0)
            ),
            "同班編成": (
                session.get("university_group_status")
                or (
                    "対象外"
                    if session.get("university_group_match") is None
                    else "適合"
                    if session.get("university_group_match")
                    else "不一致"
                )
            ),
            "文理対応": (
                session.get("field_status")
                or (
                    "対象外"
                    if session.get("field_match") is None
                    else "適合"
                    if session.get("field_match")
                    else "不一致"
                )
            ),
        }
        for session_index, session in enumerate(
            candidate.get("sessions", []), start=1
        )
    ]

    participant_summary = None
    participant_summary_columns = [
        "名前",
        "大学生役",
        "高校生役",
        "合計",
        "規定数超過",
    ]
    if candidate.get("participant_summary"):
        participant_summary = pd.DataFrame(
            candidate["participant_summary"]
        ).rename(
            columns={
                "name": "名前",
                "university_count": "大学生役",
                "high_school_count": "高校生役",
                "total_count": "合計",
                "extra_count": "規定数超過",
                "university_shortfall": "大学生役不足",
                "high_school_shortfall": "高校生役不足",
                "total_shortfall": "合計参加不足",
                "over_limit_count": "上限超過",
            }
        )
        for required_column in participant_summary_columns:
            if required_column not in participant_summary.columns:
                participant_summary[required_column] = (
                    0 if required_column != "名前" else ""
                )
        for optional_column in [
            "大学生役不足",
            "高校生役不足",
            "合計参加不足",
            "上限超過",
        ]:
            if optional_column in participant_summary.columns:
                participant_summary_columns.append(optional_column)

    result: dict[str, object] = {
        "evaluation_rows": evaluation_rows,
        "schedule_rows": schedule_rows,
        "participant_summary": participant_summary,
        "participant_summary_columns": participant_summary_columns,
    }
    cache[cache_key] = result
    while len(cache) > 32:
        cache.pop(next(iter(cache)))
    return result


def _cached_candidate_calendar_frames(
    project_id: str,
    config: Config,
    candidate: dict,
    role_display_mode: str,
    candidate_version: int | None,
) -> list[tuple[str, pd.DataFrame]]:
    cache = st.session_state.setdefault("candidate_calendar_frames_cache", {})
    key = export_cache_token(
        {
            "project_id": project_id,
            "config_version": config._storage_version,
            "candidate_version": candidate_version,
            "candidate": candidate,
            "role_display_mode": role_display_mode,
        }
    )
    if key not in cache:
        cache[key] = candidate_calendar_frames(
            config, candidate, role_display_mode
        )
        while len(cache) > 32:
            cache.pop(next(iter(cache)))
    return cache[key]


def _cached_candidate_full_calendar(
    project_id: str,
    config: Config,
    candidate: dict,
    role_display_mode: str,
    candidate_version: int | None,
) -> pd.DataFrame:
    cache = st.session_state.setdefault("candidate_full_calendar_cache", {})
    key = export_cache_token(
        {
            "project_id": project_id,
            "config_version": config._storage_version,
            "candidate_version": candidate_version,
            "candidate": candidate,
            "role_display_mode": role_display_mode,
        }
    )
    if key not in cache:
        cache[key] = candidate_full_calendar(
            config, candidate, role_display_mode
        )
        while len(cache) > 32:
            cache.pop(next(iter(cache)))
    return cache[key]


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
    publication_warnings: tuple[str, ...] | None = None,
    evaluation_context: dict[str, object] | None = None,
    candidate_for_confirmation: dict | None = None,
    policy_issues: tuple[str, ...] = (),
    *,
    operations: CandidateOperationServices,
) -> None:
    confirmation_candidate = (
        candidate if candidate_for_confirmation is None else candidate_for_confirmation
    )
    excluded_dates = set(config.excluded_dates)
    existing_excluded_dates = sorted(
        {
            str(session.get("date", ""))
            for session in candidate.get("sessions", [])
            if str(session.get("date", "")) in excluded_dates
        }
    )
    if existing_excluded_dates:
        st.warning(
            "この候補には除外日に設定された既存の開催日が含まれています。"
            "既存内容は自動削除せず、内容を確認してから扱ってください: "
            + "、".join(existing_excluded_dates)
        )
    if (
        participants is not None
        and (
            candidate.get("metrics", {}).get("evaluation_score_version")
            != EVALUATION_SCORE_VERSION
            or not _candidate_has_evaluation_config(candidate)
        )
    ):
        from schedule_adjustment_tool.domain.scheduler import (
            refresh_candidate_evaluation,
        )

        candidate = refresh_candidate_evaluation(
            candidate,
            config,
            participants,
        )
    if confirmable and publication_warnings is None:
        publication_warnings = candidate_publication_warnings(
            config,
            candidate,
            participants or [],
        )
    publication_warnings = publication_warnings or ()
    metrics = candidate["metrics"]
    strict_under_current_policy = bool(
        metrics.get("is_strict_candidate", True) and not policy_issues
    )
    stored_evaluation_config = candidate.get("evaluation_config")
    evaluation_settings = normalize_evaluation_settings(
        stored_evaluation_config.get("evaluation_settings")
        if isinstance(stored_evaluation_config, dict)
        else config.evaluation_settings
    )
    display_data = _candidate_display_data(
        project_id,
        config,
        candidate,
        candidate_version=candidate_version,
        evaluation_settings=evaluation_settings,
    )
    try:
        evaluation_score = float(metrics.get("evaluation_score"))
        evaluation_score_label = f"{evaluation_score:.1f} / 100"
    except (TypeError, ValueError):
        evaluation_score_label = "-"
    origin_kind = str(candidate.get("origin", {}).get("kind", ""))
    if origin_kind == "reoptimization":
        candidate_kind = "再最適化候補"
    elif origin_kind == "partial_optimization":
        candidate_kind = "指定を守って探索した候補"
    elif origin_kind == "manual_adjustment":
        candidate_kind = "手動調整候補"
    elif origin_kind == "manual":
        candidate_kind = "手入力候補"
    else:
        candidate_kind = (
            "手動日程"
            if metrics.get("is_manually_maintained")
            else "必須条件を満たす候補"
            if metrics.get("is_strict_candidate", True)
            else "警告付きの近似候補"
        )
    display_key = (
        f"candidate_role_display_{project_id}_{index}_"
        f"{'editable' if confirmable else 'confirmed'}"
    )
    with st.expander(
        f"候補{index}（{candidate_kind}） | 総合適合度{evaluation_score_label} | "
        f"必須条件{'満足' if strict_under_current_policy else '要確認'}",
        expanded=not confirmable if expanded is None else expanded,
    ):
        if evaluation_context:
            selected_label = str(
                evaluation_context.get("selected_label", "評価条件")
            )
            stored_label = str(
                evaluation_context.get("stored_label", "保存時の評価条件")
            )
            if evaluation_context.get("recalculated"):
                st.info(
                    f"表示中の総合適合度は{selected_label}で再評価しています。"
                    f"この候補の探索・保存時の評価条件は{stored_label}です。"
                )
            else:
                st.caption(f"評価条件: {stored_label}")
        else:
            profiles = build_evaluation_profiles([candidate], config)
            st.caption(
                "評価条件: "
                f"{profile_label_for_candidate(candidate, profiles)}"
            )
        st.caption(
            "総合適合度は100が最も設定した方針に合う候補です。"
            "必須条件の未達・超過がある候補は、総合適合度より先に区別されます。"
            "詳細のスコアはすべて、100が最も良い適合度として表示されます。"
        )
        summary_columns = st.columns(4)
        summary_columns[0].metric(
            "総合適合度（100が最良）",
            evaluation_score_label,
            help="設定した評価項目を優先度に応じて統合した、100が最良の適合度です。",
        )
        summary_columns[1].metric(
            "必須条件",
            "満足" if strict_under_current_policy else "要確認",
        )
        summary_columns[2].metric(
            "開催組数",
            metrics.get("number_of_sessions", len(candidate.get("sessions", []))),
        )
        summary_columns[3].metric(
            "規定数超過（延べ回数）",
            metrics.get("total_extra_count", 0),
            help=(
                "参加者ごとの規定数を超えた参加回数を合計した値です。"
                "超過上限の範囲内でも、この値が少ない候補を優先します。"
            ),
        )
        if strict_under_current_policy:
            st.success("必須条件: すべて満たしています")
        elif policy_issues:
            st.error(
                f"必須条件: 要確認 — 現在の条件との差異が{len(policy_issues)}件あります。"
            )
        else:
            st.error(
                "必須条件: 要確認 — "
                f"規定回数不足 {metrics.get('required_shortfall_total', 0)}回、"
                f"組内役割不足 {metrics.get('session_role_shortfall_total', 0)}人、"
                f"参加上限超過 {metrics.get('over_limit_total', 0)}回"
            )
        if metrics.get("search_phase"):
            if str(metrics.get("search_phase", "")).startswith("relaxed_"):
                violation_status = (
                    "証明済み"
                    if metrics.get("violation_minimum_proven", False)
                    else "未証明"
                )
                evaluation_status = (
                    "証明済み"
                    if metrics.get("evaluation_optimality_proven", False)
                    else "未証明"
                )
                message = (
                    f"許容違反の最小性: {violation_status} / "
                    f"同じ違反量・追加参加数での評価最適性: {evaluation_status}"
                )
                (st.info if strict_under_current_policy else st.warning)(message)
            elif strict_under_current_policy:
                if metrics.get("evaluation_optimality_proven", False):
                    st.caption("評価最適性: 証明済み")
                else:
                    st.info(
                        "成立候補は確保済みです。評価最適性は未証明です。"
                    )

        st.markdown("#### 優先度別の適合度")
        st.caption("各カードは100点満点で、100が最も良い適合度です。")
        penalties = metrics.get("evaluation_penalties", {})
        priority_penalties = metrics.get("evaluation_priority_penalties", {}) or {}
        priority_item_counts = metrics.get(
            "evaluation_priority_item_counts", {}
        ) or {}

        def evaluation_fit_score(evaluation_id: str) -> float:
            try:
                penalty = float(penalties.get(evaluation_id, 0))
            except (TypeError, ValueError):
                penalty = 0.0
            return max(0.0, min(100.0, 100.0 - penalty))

        active_priorities: list[tuple[str, float, int]] = []
        for priority in PRIORITY_LEVELS:
            enabled_items = [
                evaluation_id
                for evaluation_id, setting in evaluation_settings.items()
                if setting["enabled"] and setting["priority"] == priority
            ]
            if not enabled_items:
                continue
            group_penalty = float(priority_penalties.get(priority, 0))
            active_priorities.append(
                (
                    priority,
                    max(0.0, min(100.0, 100.0 - group_penalty)),
                    int(priority_item_counts.get(priority, len(enabled_items))),
                )
            )
        if active_priorities:
            priority_columns = st.columns(len(active_priorities))
            for column, (priority, group_score, item_count) in zip(
                priority_columns,
                active_priorities,
            ):
                with column:
                    st.metric(
                        f"{PRIORITY_LABELS[priority]}の適合度",
                        f"{group_score:.1f} / 100",
                    )
                    st.caption(f"評価項目 {item_count}件")
        else:
            st.info("評価対象の項目はありません。総合適合度は100点です。")

        st.markdown("#### 評価項目の内訳")
        evaluation_rows = display_data["evaluation_rows"]
        if evaluation_rows:
            st.dataframe(
                pd.DataFrame(evaluation_rows),
                column_config={
                    "適合度（100が最良）": st.column_config.NumberColumn(
                        "適合度（100が最良）",
                        min_value=0,
                        max_value=100,
                        format="%.1f",
                    )
                },
                hide_index=True,
                width="stretch",
            )
        else:
            st.info("評価対象の項目はありません。")

        st.markdown("#### 日程の概要")
        overview_columns = st.columns(3)
        overview_columns[0].metric(
            "使用コマ数", metrics.get("number_of_time_slots", 0)
        )
        overview_columns[1].metric(
            "Zoom開催", metrics.get("zoom_session_count", 0)
        )
        overview_columns[2].metric(
            "必要回数未達の人数", metrics.get("unmet_participant_count", 0)
        )
        if metrics.get("priority_unset_issue_count", 0):
            st.caption(
                "属性未設定のため判定できない項目があります。"
                f"対象 {metrics.get('priority_unset_issue_count', 0)}件"
            )
        rows = display_data["schedule_rows"]
        display_columns = st.columns(2)
        view_options = ["一覧表示", "カレンダー表示"]
        selected_view = display_columns[0].segmented_control(
            "表示形式",
            view_options,
            default="カレンダー表示" if calendar_first else "一覧表示",
            key=f"candidate_view_{project_id}_{index}_{confirmable}",
        )
        selected_display = display_columns[1].segmented_control(
            "役割の見分け方",
            [ROLE_DISPLAY_LABEL_OPTION, ROLE_DISPLAY_COLOR_OPTION],
            default=(
                ROLE_DISPLAY_COLOR_OPTION
                if config.role_display_mode == ROLE_DISPLAY_COLORS
                else ROLE_DISPLAY_LABEL_OPTION
            ),
            key=display_key,
        )
        role_display_mode = (
            ROLE_DISPLAY_COLORS
            if selected_display == ROLE_DISPLAY_COLOR_OPTION
            else ROLE_DISPLAY_LABELS
        )
        if role_display_mode == ROLE_DISPLAY_COLORS:
            st.markdown(
                role_display_legend_html(role_display_mode),
                unsafe_allow_html=True,
            )
        if selected_view == "カレンダー表示":
            calendar_range = st.segmented_control(
                "カレンダー範囲",
                ["週ごと", "期間全体"],
                default="週ごと",
                key=f"candidate_calendar_range_{project_id}_{index}_{confirmable}",
            )
            if calendar_range == "週ごと":
                for week_title, week_frame in _cached_candidate_calendar_frames(
                    project_id,
                    config,
                    candidate,
                    role_display_mode,
                    candidate_version,
                ):
                    st.markdown(f"##### {week_title}")
                    _render_calendar_table(
                        week_frame,
                        config=config,
                        role_display_mode=role_display_mode,
                        allow_cell_html=True,
                    )
            else:
                _render_calendar_table(
                    _cached_candidate_full_calendar(
                        project_id,
                        config,
                        candidate,
                        role_display_mode,
                        candidate_version,
                    ),
                    config=config,
                    role_display_mode=role_display_mode,
                    allow_cell_html=True,
                )
        else:
            candidate_frame = pd.DataFrame(rows)
            if role_display_mode == ROLE_DISPLAY_COLORS:
                candidate_frame = candidate_frame.style.set_properties(
                    subset=["大学生役"],
                    **{"color": UNIVERSITY_ROLE},
                ).set_properties(
                    subset=["高校生役"],
                    **{"color": HIGH_SCHOOL_ROLE},
                )
            st.dataframe(candidate_frame, hide_index=True, width="stretch")
        if display_data["participant_summary"] is not None:
            st.caption("参加者別集計")
            summary = display_data["participant_summary"]
            summary_columns = display_data["participant_summary_columns"]
            st.dataframe(
                summary[summary_columns],
                hide_index=True,
                width="stretch",
            )
        publication_acknowledged = True
        if confirmable and publication_warnings:
            st.warning("公開前に次の内容を確認してください。")
            for warning in publication_warnings:
                st.write(f"- {warning}")
            publication_acknowledged = st.checkbox(
                "上記を確認し、この候補を明示的に公開する",
                key=f"publish_warning_ack_{project_id}_{index}",
            )
        action_columns = st.columns(2)
        if allow_download:
            action_columns[0].caption(
                "Excelには日程一覧・個人別サマリー・"
                "期間全体カレンダーが含まれます。"
            )
            operations.render_prepared_download(
                action_columns[0],
                project_id=project_id,
                kind="candidate_schedule",
                cache_token=export_cache_token(
                    {
                        "config_version": config._storage_version,
                        "candidate_version": candidate_version
                        if candidate_version is not None
                        else candidate.get("storage_version", 0),
                        "candidate_position": index,
                        "participant_versions": [
                            (
                                participant.id,
                                participant.storage_version,
                                participant.updated_at,
                            )
                            for participant in (participants or [])
                        ],
                        "role_display_mode": role_display_mode,
                    }
                ),
                prepare_label="候補Excelを準備",
                download_label="候補Excelを出力",
                status_label="候補日程のExcelを準備しています...",
                build=candidate_workbook,
                build_args=(
                    candidate,
                    role_display_mode,
                    config,
                    participants or [],
                ),
                file_name=f"candidate_{index}.xlsx",
                audit_action="candidate.exported",
            )
        if confirmable and action_columns[1].button(
            "この候補で確定",
            type="primary",
            disabled=not publication_acknowledged,
            key=f"confirm_{index}",
        ):
            with status_message("候補を確定しています..."):
                confirmation_result = confirm_candidate(
                    project_id,
                    confirmation_candidate,
                    index,
                    project_status="confirmed",
                    expected_revision_id=expected_revision_id,
                    return_config_version=True,
                )
                confirmed, config_version = confirmation_result
                set_cached_confirmed(project_id, confirmed)
                if config_version is not None:
                    update_cached_config(
                        project_id,
                        {"status": "confirmed"},
                        storage_version=config_version,
                    )
                mark_manager_step_completed(project_id, "candidates")
                mark_manager_step_completed(project_id, "publish")
            st.success(f"候補{index}を確定しました。")
            st.session_state["schedule_confirm_rerender_started_at"] = (
                time_module.perf_counter()
            )
            st.rerun()

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
    operations: CandidateOperationServices,
    allow_optimization: bool = True,
    show_date_lock_controls: bool = True,
) -> None:
    configured_dates = calendar_dates_for_editing(config, initial_sessions)
    new_session_dates = set(practice_dates(config))
    if not configured_dates:
        st.warning(
            "対象日がありません。先に企画情報の設定で期間・曜日を確認してください。"
        )
    if not participants:
        st.warning(
            "参加者を選ぶため、先に参加者を名簿へ登録してください。"
        )
    edited_sessions = operations.schedule_calendar_editor(
        configured_dates,
        periods=config.enabled_periods,
        sessions=initial_sessions,
        participants=participants,
        participant_required_counts={
            participant.id: calendar_participation_limit(
                participant,
                config,
            )
            for participant in participants
        },
        participant_role_required_counts={
            participant.id: calendar_required_role_counts(
                participant,
                config,
            )
            for participant in participants
        },
        max_groups_per_slot=config.max_groups_per_slot,
        university_role_size=config.university_role_size,
        high_school_role_size=config.high_school_role_size,
        key=editor_key,
        show_optimization_controls=allow_optimization,
        show_date_lock_controls=show_date_lock_controls,
        excluded_dates=config.excluded_dates,
    )
    remaining_capacity = max(
        0,
        operations.max_stored_candidates - len(existing_candidates),
    )
    st.caption(
        f"現在の保存候補: {len(existing_candidates)}件 / "
        f"追加可能: {remaining_capacity}件。"
        "どちらの操作も既存候補を上書きしません。"
    )
    confirmation_token = export_cache_token(
        {
            "sessions": edited_sessions,
            "config": config.to_dict(),
            "participants": [
                participant.to_dict() for participant in participants
            ],
        }
    )
    allow_policy_override = st.checkbox(
        "設定・参加可能回答との差異を確認し、警告があっても候補として保存する",
        help=(
            "役割人数不足や回答外の日程などを意図的に手入力する場合だけ"
            "選択してください。探索では指定内容が必須条件になります。"
        ),
        key=f"{editor_key}_policy_override_{confirmation_token}",
    )
    action_columns = st.columns(2 if allow_optimization else 1)
    direct_clicked = action_columns[0].button(
        direct_button_label,
        type="primary",
        disabled=(
            not configured_dates
            or not participants
            or remaining_capacity == 0
        ),
        key=f"{editor_key}_save_direct",
    )
    optimize_clicked = False
    if allow_optimization:
        optimize_clicked = action_columns[1].button(
            optimization_button_label,
            disabled=(
                not new_session_dates
                or not ready
                or remaining_capacity == 0
            ),
            key=f"{editor_key}_optimize",
        )
    if remaining_capacity == 0:
        st.warning(
            "保存候補が上限に達しているため追加できません。"
            "候補を整理してから実行してください。"
        )

    if direct_clicked:
        candidate, errors, policy_issues = candidate_from_calendar_sessions(
            base_candidate,
            edited_sessions,
            config,
            participants,
            origin=direct_origin,
        )
        for error in errors:
            st.error(error)
        if policy_issues and not allow_policy_override:
            for issue in policy_issues:
                st.warning(issue)
            st.info(
                "内容が意図どおりなら確認欄を選び、もう一度追加してください。"
            )
        elif candidate is not None and not errors:
            for issue in policy_issues:
                st.warning(issue)
            (
                merged_candidates,
                added_candidates,
                duplicate_count,
                capacity_skipped_count,
            ) = operations.append_project_candidates(
                project_id,
                existing_candidates,
                [candidate],
            )
            if added_candidates:
                st.session_state[f"{editor_key}_feedback"] = {
                    "success": (
                        "カレンダーの内容を新しい候補として追加しました。"
                        f" 保存候補は合計{len(merged_candidates)}件です。"
                    ),
                    "warnings": list(policy_issues),
                }
                st.rerun()
            if duplicate_count:
                st.info("保存済みと同一のため重複保存しませんでした。")
            if capacity_skipped_count:
                st.warning("保存上限のため候補を追加できませんでした。")

    if optimize_clicked and allow_optimization:
        assignment_locks, lock_errors = (
            assignment_locks_from_calendar_sessions(
                edited_sessions,
                participants,
            )
        )
        for error in lock_errors:
            st.error(error)
        if not lock_errors and not assignment_locks:
            st.warning(
                "探索で守る指定がありません。日時・形式・参加者のいずれかを指定してください。"
            )
        elif not lock_errors:
            preview_candidate, preview_errors, preview_policy_issues = (
                candidate_from_calendar_sessions(
                    base_candidate,
                    edited_sessions,
                    config,
                    participants,
                    origin=optimization_origin,
                    allow_solver_completion=True,
                )
            )
            for error in preview_errors:
                st.error(error)
            if preview_errors:
                st.error("構造上のエラーがあるため、探索を開始できません。")
                return
            if preview_policy_issues and not allow_policy_override:
                for issue in preview_policy_issues:
                    st.warning(issue)
                st.info(
                    "指定内容の警告を確認し、確認欄を選んでから"
                    "もう一度探索してください。"
                )
                return
            with st.spinner(
                f"最大{search_timeout}秒で、指定を守る候補を"
                f"最大{min(search_count, remaining_capacity)}件探索しています..."
            ):
                new_candidates, reasons, lock_error = (
                    operations.generate_candidates_with_assignment_locks(
                        project_id,
                        config,
                        participants,
                        existing_candidates,
                        assignment_locks,
                        candidate_limit=min(
                            search_count,
                            remaining_capacity,
                        ),
                        timeout_seconds=search_timeout,
                        random_seed=search_seed,
                    )
                )
            if lock_error:
                st.warning(lock_error)
            else:
                for candidate in new_candidates:
                    candidate["origin"] = {
                        **optimization_origin,
                        "created_at": now_iso(),
                    }
                (
                    merged_candidates,
                    added_candidates,
                    duplicate_count,
                    capacity_skipped_count,
                ) = operations.append_project_candidates(
                    project_id,
                    existing_candidates,
                    new_candidates,
                )
                st.session_state["candidate_reasons"] = reasons
                if added_candidates:
                    st.session_state[f"{editor_key}_feedback"] = {
                        "success": (
                            "指定を守る候補を"
                            f"{len(added_candidates)}件追加しました。"
                            f" 保存候補は合計{len(merged_candidates)}件です。"
                        ),
                        "warnings": list(reasons)
                        + list(preview_policy_issues)
                        + ([
                            "必須条件を満たさない近似候補を保存しました。"
                            "不足・超過の内容を確認してください。"
                        ] if any(
                            not candidate.get("metrics", {}).get(
                                "is_strict_candidate", True
                            )
                            for candidate in added_candidates
                        ) else []),
                    }
                    st.rerun()
                for reason in reasons:
                    st.warning(reason)
                if duplicate_count:
                    st.info(
                        f"保存済みと同一の候補{duplicate_count}件は"
                        "重複保存しませんでした。"
                    )
                if capacity_skipped_count:
                    st.warning(
                        f"保存上限のため{capacity_skipped_count}件を"
                        "追加できませんでした。"
                    )
                if not new_candidates and not reasons:
                    st.warning(
                        "指定を満たす新しい候補は見つかりませんでした。"
                    )

    _render_candidate_operation_feedback(editor_key)
