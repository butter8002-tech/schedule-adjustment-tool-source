"""Candidate review, adjustment, and publication screens for the manager UI."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.domain.models import Config, Participant
from schedule_adjustment_tool.ui.manager.candidate_evaluation import (
    EvaluationProfile,
    evaluate_candidates_for_profile,
    evaluation_context,
    profile_label_for_candidate,
    render_evaluation_profile_selector,
)


@dataclass(frozen=True)
class CandidateScreenServices:
    """Entry-point operations used by candidate screens.

    Candidate generation, persistence, and publication stay in the app's
    existing business functions.  This module only arranges the focused UI
    screens around those operations.
    """

    max_candidates_per_search: int
    max_search_seconds: int
    cached_candidates: Callable[[str], list[dict]]
    refresh_saved_candidate_evaluations: Callable[
        [str, Config, list[Participant], list[dict]], list[dict]
    ]
    show_candidate: Callable[..., None]
    scheduling_target_participants: Callable[
        [list[Participant]], list[Participant]
    ]
    schedule_calendar_initial_sessions: Callable[..., list[dict]]
    render_candidate_calendar_actions: Callable[..., None]
    calendar_candidate_token: Callable[[dict], str]
    candidate_storage_version: Callable[[str], int] | None = None


def candidate_comparison_rows(
    candidates: list[dict],
    *,
    source_candidates: list[dict] | None = None,
    profiles: list[EvaluationProfile] | None = None,
) -> list[dict[str, object]]:
    original_candidates = source_candidates or candidates
    rows: list[dict[str, object]] = []
    for index, candidate in enumerate(candidates, start=1):
        source_candidate = original_candidates[index - 1]
        metrics = candidate.get("metrics", {})
        origin_kind = str(candidate.get("origin", {}).get("kind", ""))
        origin_label = {
            "manual": "手動調整",
            "manual_adjustment": "手動調整",
            "partial_optimization": "手動調整+最適化探索",
            "reoptimization": "確定日程から再最適化",
        }.get(origin_kind, "最適化探索")
        if not metrics.get("search_phase"):
            search_result_label = "保存済み"
        elif str(metrics.get("search_phase", "")).startswith("relaxed_"):
            search_result_label = (
                "近似探索・違反最小証明済み"
                if metrics.get("violation_minimum_proven", False)
                else "近似探索・違反最小未証明"
            )
        elif metrics.get("is_strict_candidate", True):
            search_result_label = (
                "成立・評価最適性証明済み"
                if metrics.get("evaluation_optimality_proven", False)
                else "成立・評価最適性未証明"
            )
        else:
            search_result_label = "警告付きの近似候補"
        rows.append(
            {
                "候補": index,
                "作成方法": origin_label,
                "最適化した評価条件": (
                    ""
                    if origin_kind in {"manual", "manual_adjustment"}
                    else profile_label_for_candidate(
                        source_candidate,
                        profiles or [],
                    )
                ),
                "総合適合度": metrics.get("evaluation_score", 0),
                "必須条件": (
                    "満足"
                    if metrics.get("is_strict_candidate", True)
                    else "要確認"
                ),
                "探索結果": search_result_label,
                "開催組数": metrics.get("number_of_sessions", 0),
                "規定数超過（延べ回数）": metrics.get("total_extra_count", 0),
            }
        )
    return rows


def selected_candidate_position(
    project_id: str,
    candidates: list[dict],
    rows: list[dict[str, object]],
    *,
    label: str,
) -> int:
    return int(
        st.selectbox(
            label,
            list(range(len(candidates))),
            format_func=lambda position: (
                f"候補{position + 1} / "
                f"{rows[position]['作成方法']} / "
                f"総合適合度{rows[position]['総合適合度']} / "
                f"必須条件{rows[position]['必須条件']} / "
                f"開催組数{rows[position]['開催組数']}"
            ),
            key=f"selected_candidate_{project_id}",
        )
    )


def render_candidate_list_screen(
    project_id: str,
    config: Config,
    participants: list[Participant],
    *,
    services: CandidateScreenServices,
) -> None:
    candidates = services.refresh_saved_candidate_evaluations(
        project_id,
        config,
        participants,
        services.cached_candidates(project_id),
    )
    if not candidates:
        st.info("候補はまだ作成されていません。")
        return
    candidate_version = (
        services.candidate_storage_version(project_id)
        if services.candidate_storage_version is not None
        else 0
    )
    selected_profile, profiles = render_evaluation_profile_selector(
        project_id,
        candidates,
        config,
        key_suffix="list",
    )
    display_candidates = evaluate_candidates_for_profile(
        project_id,
        candidates,
        config,
        participants,
        selected_profile,
        candidate_version=candidate_version,
    )
    rows = candidate_comparison_rows(
        display_candidates,
        source_candidates=candidates,
        profiles=profiles,
    )
    st.caption(
        "まず必須条件、次に総合適合度を比較し、詳しく確認する候補を選んでください。"
    )
    st.dataframe(
        pd.DataFrame(rows),
        column_config={
            "総合適合度": st.column_config.NumberColumn(
                "総合適合度（/100・100が最良）",
                min_value=0,
                max_value=100,
                format="%.1f",
            ),
        },
        hide_index=True,
        width="stretch",
    )
    selected_position = selected_candidate_position(
        project_id,
        candidates,
        rows,
        label="詳しく見る候補",
    )
    st.divider()
    st.markdown(f"#### 候補{selected_position + 1}の詳細")
    services.show_candidate(
        project_id,
        config,
        display_candidates[selected_position],
        selected_position + 1,
        confirmable=False,
        participants=participants,
        candidate_version=candidate_version,
        evaluation_context=evaluation_context(
            candidates[selected_position],
            selected_profile,
            profiles,
        ),
        candidate_for_confirmation=candidates[selected_position],
    )


def render_candidate_adjustment_screen(
    project_id: str,
    config: Config,
    participants: list[Participant],
    *,
    services: CandidateScreenServices,
) -> None:
    candidates = services.refresh_saved_candidate_evaluations(
        project_id,
        config,
        participants,
        services.cached_candidates(project_id),
    )
    if not candidates:
        st.info("調整する候補がありません。先に候補を作成してください。")
        return
    candidate_version = (
        services.candidate_storage_version(project_id)
        if services.candidate_storage_version is not None
        else 0
    )
    selected_profile, profiles = render_evaluation_profile_selector(
        project_id,
        candidates,
        config,
        key_suffix="adjustment",
    )
    display_candidates = evaluate_candidates_for_profile(
        project_id,
        candidates,
        config,
        participants,
        selected_profile,
        candidate_version=candidate_version,
    )
    rows = candidate_comparison_rows(
        display_candidates,
        source_candidates=candidates,
        profiles=profiles,
    )
    selected_position = selected_candidate_position(
        project_id,
        candidates,
        rows,
        label="調整する候補",
    )
    selected_candidate = candidates[selected_position]
    st.caption(
        "元の候補を残したまま、調整した内容を新しい候補として保存します。"
    )
    target = services.scheduling_target_participants(participants)
    ready = bool(target) and all(
        participant.input_status == "submitted" for participant in target
    )
    initial_sessions = services.schedule_calendar_initial_sessions(
        selected_candidate,
        participants,
        lock_sessions=True,
        lock_meeting_modes=False,
        lock_members=True,
        reset_roles=False,
    )
    services.render_candidate_calendar_actions(
        project_id,
        config,
        participants,
        candidates,
        base_candidate=selected_candidate,
        initial_sessions=initial_sessions,
        editor_key=(
            f"focused_candidate_adjustment_{project_id}_"
            f"{services.calendar_candidate_token(selected_candidate)}"
        ),
        direct_origin={
            "kind": "manual_adjustment",
            "base_candidate_position": selected_position + 1,
        },
        optimization_origin={
            "kind": "partial_optimization",
            "base_candidate_position": selected_position + 1,
        },
        direct_button_label="調整内容を新しい候補として追加",
        optimization_button_label="指定を守って探索",
        search_count=min(
            config.max_candidates,
            services.max_candidates_per_search,
        ),
        search_timeout=min(
            config.search_timeout_seconds,
            services.max_search_seconds,
        ),
        search_seed=(
            config.random_seed + len(candidates) + selected_position + 1
        ),
        ready=ready,
        allow_optimization=False,
    )


def render_candidate_publish_screen(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None,
    *,
    services: CandidateScreenServices,
) -> None:
    if config.status == "confirmed" and confirmed:
        st.success("日程はすでに公開されています。")
        st.info(
            "公開中の日程は「公開中の日程」で確認できます。"
            "変更する場合は「公開後の変更」へ進んでください。"
        )
        return
    candidates = services.refresh_saved_candidate_evaluations(
        project_id,
        config,
        participants,
        services.cached_candidates(project_id),
    )
    if not candidates:
        st.info("公開できる候補がありません。先に候補を作成してください。")
        return
    candidate_version = (
        services.candidate_storage_version(project_id)
        if services.candidate_storage_version is not None
        else 0
    )
    selected_profile, profiles = render_evaluation_profile_selector(
        project_id,
        candidates,
        config,
        key_suffix="publish",
    )
    display_candidates = evaluate_candidates_for_profile(
        project_id,
        candidates,
        config,
        participants,
        selected_profile,
        candidate_version=candidate_version,
    )
    rows = candidate_comparison_rows(
        display_candidates,
        source_candidates=candidates,
        profiles=profiles,
    )
    selected_position = selected_candidate_position(
        project_id,
        candidates,
        rows,
        label="公開する候補",
    )
    selected_candidate = candidates[selected_position]
    st.caption(
        "公開前は、必須条件が満足であること、総合適合度、評価内訳、日程一覧の順に確認してください。"
    )
    services.show_candidate(
        project_id,
        config,
        display_candidates[selected_position],
        selected_position + 1,
        confirmable=True,
        expected_revision_id=str(
            (confirmed or {}).get("schedule_revision", {}).get("id", "")
        ),
        participants=participants,
        candidate_version=candidate_version,
        evaluation_context=evaluation_context(
            selected_candidate,
            selected_profile,
            profiles,
        ),
        candidate_for_confirmation=selected_candidate,
        calendar_first=True,
        expanded=True,
    )
