"""Candidate generation and saved-candidate comparison UI."""

from __future__ import annotations

import time as time_module
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.domain.evaluation_config import EVALUATION_SCORE_VERSION
from schedule_adjustment_tool.domain.models import (
    CANDIDATE_SEARCH_MODE_AUTO,
    Config,
    Participant,
)
from schedule_adjustment_tool.storage import (
    acquire_job_lock,
    load_cross_project_blocked_slots,
    release_job_lock,
    save_candidates,
)
from schedule_adjustment_tool.ui.manager.app_cache import status_message
from schedule_adjustment_tool.ui.manager.candidate_calendar import (
    schedule_calendar_initial_sessions,
)
from schedule_adjustment_tool.ui.manager.candidate_evaluation import (
    LEGACY_EVALUATION_SOURCE,
    evaluate_candidates_for_profile,
    evaluation_context,
    render_evaluation_profile_selector,
)
from schedule_adjustment_tool.ui.manager.candidates import candidate_comparison_rows
from schedule_adjustment_tool.ui.manager.project_cache import (
    cached_candidates,
    candidate_storage_version,
    set_cached_candidates,
)
from schedule_adjustment_tool.ui.manager.session_state import (
    mark_manager_step_started,
)
from schedule_adjustment_tool.ui.manager.version_update_notices import (
    render_legacy_candidate_evaluation_notice,
)
from schedule_adjustment_tool.ui.presentation import (
    CANDIDATE_SEARCH_MODE_LABELS,
)


@dataclass(frozen=True)
class CandidateGenerationServices:
    """Operations supplied by the application entry point.

    The screen coordinates existing solver and confirmation behavior without
    owning persistence, the solver's constraints, or dialog implementations.
    """

    max_stored_candidates: int
    scheduler_tools: Callable[[], dict[str, Callable]]
    render_search_settings: Callable[..., tuple[int, int, int, str]]
    scheduling_target_participants: Callable[
        [list[Participant]], list[Participant]
    ]
    confirm_replacement: Callable[[str, str, int], None]
    render_calendar_actions: Callable[..., None]
    show_candidate: Callable[..., None]
    calendar_candidate_token: Callable[[dict], str]


def render_candidate_search_feedback(
    reasons: list[str],
    *,
    title: str = "候補が見つかりませんでした。",
    warning: bool = False,
) -> None:
    message = st.warning if warning else st.error
    message(title)
    if reasons:
        for reason in reasons:
            st.write(f"- {reason}")
    else:
        st.write(
            "- 参加可能時間、必要回数、役割人数、1日上限、"
            "連続コマ禁止の設定を確認してください。"
        )


def render_candidates(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None = None,
    *,
    creation_only: bool = False,
    operations: CandidateGenerationServices,
) -> None:
    tools = operations.scheduler_tools()
    st.header("候補生成")
    st.caption(
        "①候補を作る → ②保存候補を比較する → "
        "③選んだ候補を確認・調整・確定する、の順に操作します。"
    )
    st.subheader("1. 新しい候補を作る")
    creation_method = st.segmented_control(
        "候補の作り方",
        ["最適化探索", "手動+自動調整"],
        default="最適化探索",
        key=f"candidate_creation_method_{project_id}",
        help=(
            "手動調整した内容をそのまま保存するか、指定を守って探索できます。"
        ),
    )
    search_count, search_timeout, search_seed, search_mode = (
        operations.render_search_settings(project_id, config)
    )
    search_mode_label = CANDIDATE_SEARCH_MODE_LABELS.get(
        search_mode,
        CANDIDATE_SEARCH_MODE_LABELS[CANDIDATE_SEARCH_MODE_AUTO],
    )
    st.caption(
        "現在の探索モード: "
        f"{search_mode_label}"
    )
    target = operations.scheduling_target_participants(participants)
    blockers = tools["diagnose_infeasibility"](config, participants)
    ready = bool(target) and all(
        participant.input_status == "submitted" for participant in target
    )
    if not ready:
        if creation_method == "最適化探索":
            st.warning("自動探索を開始できません。")
        else:
            st.info(
                "自動探索は開始できません。"
                "手動調整内容の保存は、カレンダー下の条件を満たしていれば利用できます。"
            )
        for reason in blockers:
            st.write(f"- {reason}")
    existing_candidates = cached_candidates(project_id)
    render_legacy_candidate_evaluation_notice(project_id, existing_candidates)
    replace_requested = False
    additional_clicked = False
    search_attempted = False
    if creation_method == "最適化探索":
        st.caption(
            "必須条件を満たす候補は先に確保し、残り時間で改善します。"
            "近似では、許容違反、不要な追加参加、総合適合度の順を崩さずに探します。"
        )
        action_columns = st.columns(2)
        if existing_candidates:
            additional_clicked = action_columns[0].button(
                "追加で探索（保存候補は残す）",
                type="primary",
                disabled=not ready,
            )
            replace_requested = action_columns[1].button(
                "最初から探索（保存候補を置き換える）",
                disabled=not ready,
            )
        else:
            replace_requested = action_columns[0].button(
                "最適化探索を開始",
                type="primary",
                disabled=not ready,
            )
            action_columns[1].caption(
                "保存候補ができると、既存候補を残した追加探索を選べます。"
            )
    replacement_approved = bool(
        st.session_state.pop(
            f"candidate_replacement_approved_{project_id}", False
        )
    )
    generate_clicked = replacement_approved or (
        replace_requested and not existing_candidates
    )
    if replace_requested and existing_candidates:
        operations.confirm_replacement(
            project_id,
            config.title,
            len(existing_candidates),
        )
    if generate_clicked or additional_clicked:
        last_search_at = float(st.session_state.get("last_search_at", 0.0))
        if time_module.monotonic() - last_search_at < 5:
            st.warning("探索の再実行は5秒以上あけてください。")
            return
        st.session_state["last_search_at"] = time_module.monotonic()
        search_owner = st.session_state.setdefault(
            "search_owner_id", uuid4().hex
        )
        if not acquire_job_lock(
            project_id, search_owner, search_timeout + 30
        ):
            st.warning("この企画では別の探索が実行中です。")
            return
        excluded = existing_candidates if additional_clicked else []
        try:
            with st.spinner(
                f"最大{search_timeout}秒で、新しい候補を最大{search_count}件探索しています..."
            ):
                target_ids = [
                    participant.id
                    for participant in operations.scheduling_target_participants(participants)
                ]
                blocked_slots_by_participant = (
                    load_cross_project_blocked_slots(project_id, target_ids)
                )
                new_candidates, reasons = tools["generate_candidates"](
                    config,
                    participants,
                    timeout_seconds=search_timeout,
                    random_seed=search_seed
                    + (len(existing_candidates) if additional_clicked else 0),
                    candidate_limit=search_count,
                    excluded_candidates=excluded,
                    blocked_slots_by_participant=blocked_slots_by_participant,
                )
            search_attempted = True
            if additional_clicked:
                unique_candidates = {
                    tools["candidate_fingerprint"](candidate): candidate
                    for candidate in existing_candidates
                }
                for candidate in new_candidates:
                    unique_candidates.setdefault(
                        tools["candidate_fingerprint"](candidate), candidate
                    )
                candidates = sorted(
                    unique_candidates.values(), key=tools["candidate_sort_key"]
                )
            else:
                candidates = new_candidates
            if new_candidates:
                candidates_before_limit = len(candidates)
                candidates = candidates[: operations.max_stored_candidates]
                discarded_candidate_count = (
                    candidates_before_limit - len(candidates)
                )
                with status_message("候補を保存しています..."):
                    version = save_candidates(
                        project_id,
                        candidates,
                        expected_version=candidate_storage_version(project_id),
                    )
                    set_cached_candidates(project_id, candidates, version=version)
            else:
                # 置き換え探索が失敗しても、既存候補を消さない。
                candidates = existing_candidates
            if new_candidates:
                mark_manager_step_started(project_id, "candidates")
        finally:
            release_job_lock(project_id, search_owner)
        st.session_state["candidate_reasons"] = reasons
        if new_candidates:
            action_label = "追加" if additional_clicked else "保存"
            st.success(
                f"新しい候補を{len(new_candidates)}件{action_label}しました。"
                f" 現在の保存候補は{len(candidates)}件です。"
            )
            if discarded_candidate_count:
                st.info(
                    f"Cloud負荷を抑えるため、評価上位"
                    f"{operations.max_stored_candidates}件を保存し、"
                    f"{discarded_candidate_count}件を保存対象外にしました。"
                )
            has_approximate_candidate = any(
                not candidate["metrics"].get("is_strict_candidate", True)
                for candidate in new_candidates
            )
            if reasons:
                for reason in reasons:
                    st.warning(reason)
            elif has_approximate_candidate:
                st.warning(
                    "必須条件を満たさない近似候補を保存しました。"
                    "不足・超過の内容を確認してください。"
                )
        elif search_attempted:
            render_candidate_search_feedback(
                reasons,
                title=(
                    "新しい候補が見つかりませんでした。"
                    if existing_candidates
                    else "候補が見つかりませんでした。"
                ),
                warning=bool(existing_candidates),
            )

    candidates = cached_candidates(project_id)
    if any(
        candidate.get("metrics", {}).get("evaluation_score_version")
        != EVALUATION_SCORE_VERSION
        or not tools["candidate_has_evaluation_config"](candidate)
        for candidate in candidates
    ):
        with status_message("保存済み候補の評価を更新しています..."):
            for candidate in candidates:
                if not tools["candidate_has_evaluation_config"](candidate):
                    candidate["evaluation_config_source"] = LEGACY_EVALUATION_SOURCE
            candidates = [
                tools["refresh_candidate_evaluation"](candidate, config, participants)
                for candidate in candidates
            ]
            candidates.sort(key=tools["candidate_sort_key"])
            version = save_candidates(
                project_id,
                candidates,
                expected_version=candidate_storage_version(project_id),
            )
            set_cached_candidates(project_id, candidates, version=version)
    if creation_method == "手動+自動調整":
        with st.container(border=True):
            st.markdown("#### カレンダーで候補を作る")
            st.caption(
                "参加者を選び、必要な役割だけ指定します。"
            )
            operations.render_calendar_actions(
                project_id,
                config,
                participants,
                candidates,
                base_candidate=None,
                initial_sessions=[],
                editor_key=f"manual_candidate_calendar_{project_id}",
                direct_origin={"kind": "manual"},
                optimization_origin={
                    "kind": "partial_optimization",
                    "base": "manual",
                },
                direct_button_label="手動調整内容を新しい候補として追加",
                optimization_button_label="指定を守って探索",
                search_count=search_count,
                search_timeout=search_timeout,
                search_seed=search_seed + len(candidates),
                ready=ready,
                show_date_lock_controls=False,
            )
    if creation_only:
        return
    st.divider()
    st.subheader("2. 保存候補を比較する")
    reasons = st.session_state.get("candidate_reasons", [])
    if not candidates:
        if search_attempted:
            return
        if reasons:
            st.error("条件を満たす候補が見つかりませんでした。")
            for reason in reasons:
                st.write(f"- {reason}")
        else:
            st.info("候補はまだ生成されていません。")
        return
    st.caption(
        f"保存候補 {len(candidates)}件。詳細を表示する候補を1件選択してください。"
    )
    st.info(
        "まず必須条件が「満足」かを確認し、その中で総合適合度の高い候補を見ます。"
        "総合適合度は100点満点で100が最良、規定数超過（延べ回数）は少ないほど良い値です。"
    )
    search_metrics = candidates[0].get("metrics", {})
    search_termination_status = search_metrics.get("search_termination_status")
    if search_termination_status in {"TIME_LIMIT", "UNKNOWN"}:
        st.info(
            "探索は時間上限で終了しました。"
            f"要求{search_metrics.get('search_requested_candidate_count', '-')}件に対し、"
            f"{search_metrics.get('search_returned_candidate_count', len(candidates))}件を取得しています。"
            + (
                "必須条件を満たす候補は確保していますが、評価改善や"
                "すべての候補を探し終えたとは限りません。"
                if search_metrics.get("is_strict_candidate", True)
                else "表示した近似候補の不足内容を確認してください。"
                "違反最小性やすべての候補の探索は未完了の場合があります。"
            )
        )
    elif search_termination_status == "EXHAUSTED":
        st.caption("条件を満たす異なる候補をすべて列挙しました。")
    if search_metrics.get("search_phase"):
        if str(search_metrics.get("search_phase", "")).startswith("relaxed_"):
            violation_proven = search_metrics.get(
                "violation_minimum_proven", False
            )
            if search_metrics.get("is_strict_candidate", True):
                st.success(
                    "近似モードで必須条件を満たす候補が見つかりました。"
                    f"許容違反の最小性は{'証明済み' if violation_proven else '未証明'}です。"
                )
            elif violation_proven:
                st.warning(
                    "警告付きの近似候補です。許容違反が最小であることは証明済みです。"
                )
            else:
                st.warning(
                    "警告付きの近似候補です。時間内に見つかった最良候補ですが、"
                    "許容違反が最小であることは未証明です。"
                )
        elif search_metrics.get("is_strict_candidate", True):
            if search_metrics.get("evaluation_optimality_proven", False):
                st.success(
                    "必須条件を満たす候補を確保し、追加参加を最小にした中で"
                    "評価最適性を証明しました。"
                )
            else:
                st.info(
                    "必須条件を満たす候補を確保しました。評価最適性は未証明です。"
                )
        else:
            st.warning(
                "警告付きの近似候補です。不足内容を確認してください。"
            )
    selected_profile, profiles = render_evaluation_profile_selector(
        project_id,
        candidates,
        config,
        key_suffix="generation",
    )
    display_candidates = evaluate_candidates_for_profile(
        project_id,
        candidates,
        config,
        participants,
        selected_profile,
        candidate_version=candidate_storage_version(project_id),
    )
    summary_rows = candidate_comparison_rows(
        display_candidates,
        source_candidates=candidates,
        profiles=profiles,
    )
    st.dataframe(
        pd.DataFrame(summary_rows),
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
    st.subheader("3. 選択した候補を確認・調整・確定する")
    selected_position = st.selectbox(
        "詳細表示する候補",
        list(range(len(candidates))),
        format_func=lambda position: (
            f"候補{position + 1} / "
            f"{summary_rows[position]['作成方法']} / "
            f"総合適合度{summary_rows[position]['総合適合度']} / "
            f"必須条件{summary_rows[position]['必須条件']} / "
            f"開催組数{summary_rows[position]['開催組数']}"
        ),
        key=f"selected_candidate_{project_id}",
    )
    selected_candidate = candidates[selected_position]
    display_selected_candidate = display_candidates[selected_position]
    if config.status == "confirmed":
        st.info(
            "公開後の候補はここから直接公開できません。"
            "「確定日程の改訂」で公開版から派生する改訂案を作成してください。"
        )
    operations.show_candidate(
        project_id,
        config,
        display_selected_candidate,
        selected_position + 1,
        confirmable=config.status != "confirmed",
        expected_revision_id=str(
            (confirmed or {}).get("schedule_revision", {}).get("id", "")
        ),
        participants=participants,
        evaluation_context=evaluation_context(
            selected_candidate,
            selected_profile,
            profiles,
        ),
        candidate_for_confirmation=selected_candidate,
    )
    with st.expander(
        "候補を複製して手動調整（元候補は保持）"
    ):
        st.caption(
            "表示中の候補を複製して編集します。元の候補は残り、"
            "手動調整結果を新しい候補として末尾へ追加します。"
        )
        candidate_token = operations.calendar_candidate_token(selected_candidate)
        initial_sessions = schedule_calendar_initial_sessions(
            selected_candidate,
            participants,
            lock_sessions=True,
            lock_meeting_modes=False,
            lock_members=True,
            reset_roles=False,
        )
        operations.render_calendar_actions(
            project_id,
            config,
            participants,
            candidates,
            base_candidate=selected_candidate,
            initial_sessions=initial_sessions,
            editor_key=(
                f"candidate_adjustment_calendar_{project_id}_{candidate_token}"
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
            search_count=search_count,
            search_timeout=search_timeout,
            search_seed=search_seed + len(candidates) + selected_position + 1,
            ready=ready,
            allow_optimization=False,
        )
