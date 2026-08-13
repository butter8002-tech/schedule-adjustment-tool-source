"""Post-publication amendment workspace UI.

This module coordinates request intake, draft creation, comparison, reply
tracking, and publication. Storage owns revisions, workspace versions, CAS,
and transactions; solver behavior remains supplied by the application entry
point.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.domain.amendments import (
    active_amendment,
    amendment_requester_ids,
    amendment_requests,
    amendment_unavailable_slots_by_participant,
    build_dm_messages,
    format_dm_option,
    participant_schedule_changes,
)
from schedule_adjustment_tool.domain.models import (
    Config,
    Participant,
    format_slot,
    make_slot_key,
)
from schedule_adjustment_tool.storage import (
    StorageConflictError,
    StorageError,
    acquire_job_lock,
    load_cross_project_blocked_slots,
    load_schedule_amendment_workspace,
    load_schedule_revision,
    release_job_lock,
    save_config_fields,
    save_schedule_amendment_drafts,
    save_schedule_amendment_reply,
    save_schedule_amendment_request,
    select_schedule_amendment_proposals,
)
from schedule_adjustment_tool.ui.manager.app_cache import (
    export_cache_token,
    status_message,
)
from schedule_adjustment_tool.ui.manager.amendments import (
    amendment_change_rows,
    amendment_reply_rows,
    render_amendment_dm_template_tab,
    render_amendment_unavailable_calendar,
    schedule_amendment_discard_confirmation_dialog,
    schedule_amendment_publish_confirmation_dialog,
)
from schedule_adjustment_tool.ui.manager.candidate_calendar import (
    calendar_required_role_counts,
    calendar_dates_for_editing,
    calendar_participation_limit,
    candidate_from_calendar_sessions,
    schedule_calendar_initial_sessions,
)
from schedule_adjustment_tool.ui.manager.project_cache import (
    refresh_project_data_cache,
    update_cached_config,
)


@dataclass(frozen=True)
class AmendmentWorkspaceServices:
    """Entry-point behavior used by the amendment workspace UI."""

    max_search_seconds: int
    changed_config_updates: Callable[[Config, dict], dict]
    scheduler_tools: Callable[[], dict[str, Callable]]
    scheduling_target_participants: Callable[
        [list[Participant]], list[Participant]
    ]
    schedule_calendar_editor: Callable[..., list[dict]]
    show_candidate: Callable[..., None]

def render_amendment_requests_tab(
    project_id: str,
    config: Config,
    participants: list[Participant],
    amendment: dict | None,
    *,
    current_revision_id: str,
    public_version: int,
    workspace_version: int,
    operations: AmendmentWorkspaceServices,
) -> None:
    participant_by_id = {
        participant.id: participant for participant in participants
    }
    participant_names = {
        participant.id: participant.name for participant in participants
    }
    target_participants = operations.scheduling_target_participants(participants)
    if not target_participants:
        st.warning("変更依頼者として選べる参加者がいません。")
        return

    base_revision_id = (
        str(amendment.get("base_revision_id", ""))
        if amendment is not None
        else current_revision_id
    )
    if amendment is not None:
        amendment_id = str(amendment["id"])
        requests = amendment_requests(amendment)
        st.write(
            f"改訂元: **公開版v{public_version}** / "
            f"登録済み: **{len(requests)}件**"
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "変更依頼者": request.get(
                            "requester_name",
                            participant_names.get(
                                str(request.get("requester_id", "")),
                                "",
                            ),
                        ),
                        "新たに不可能になったコマ": "／".join(
                            format_slot(slot)
                            for slot in request.get(
                                "unavailable_slots",
                                [],
                            )
                        ),
                        "メモ": request.get("reason", ""),
                    }
                    for request in requests
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        if base_revision_id != current_revision_id:
            st.error(
                "改訂開始後に公開版が変更されました。"
                "この改訂案は公開できないため、破棄して"
                "現在版から作り直してください。"
            )
        if st.button(
            "この改訂作業を破棄",
            key=f"discard_schedule_amendment_{amendment_id}",
        ):
            schedule_amendment_discard_confirmation_dialog(
                project_id,
                amendment_id,
                workspace_version,
            )
        request_container = st.expander(
            "別の変更依頼を追加",
            expanded=False,
        )
        amendment_token = amendment_id
    else:
        requests = []
        st.caption(
            "依頼された不可能コマをすべて登録します。"
            "複数人から依頼がある場合は、1件保存した後に"
            "続けて追加できます。"
        )
        request_container = st.container(border=True)
        amendment_token = "new"

    with request_container:
        requester_id = st.selectbox(
            "変更依頼者",
            [participant.id for participant in target_participants],
            format_func=lambda value: participant_names.get(value, value),
            key=(
                f"amendment_requester_{project_id}_"
                f"{amendment_token}"
            ),
        )
        requester = participant_by_id[requester_id]
        st.markdown("##### 新たに不可能になったコマ")
        requested_by_participant = (
            amendment_unavailable_slots_by_participant(amendment)
            if amendment is not None
            else {}
        )
        unavailable_slots = render_amendment_unavailable_calendar(
            project_id,
            config,
            requester,
            amendment_token=amendment_token,
            already_requested_slots=set(
                requested_by_participant.get(requester_id, [])
            ),
        )
        reason = st.text_area(
            "メモ",
            key=(
                f"amendment_request_reason_{project_id}_"
                f"{amendment_token}_{requester_id}"
            ),
            height=80,
        )
        if (
            amendment is not None
            and amendment.get("proposal_revision_ids")
        ):
            st.warning(
                "この依頼を追加すると、現在の改訂案は比較対象から"
                "外れます。改訂案の履歴自体は削除されません。"
            )
        add_request_label = (
            "この変更依頼を追加"
            if amendment is not None
            else "変更依頼を保存して改訂を開始"
        )
        if st.button(
            add_request_label,
            type="primary",
            disabled=(
                not unavailable_slots
                or base_revision_id != current_revision_id
            ),
            key=(
                f"save_schedule_amendment_request_{project_id}_"
                f"{amendment_token}_{requester_id}"
            ),
        ):
            try:
                with status_message("変更依頼を保存しています..."):
                    save_schedule_amendment_request(
                        project_id,
                        requester_id,
                        unavailable_slots,
                        reason=reason,
                        expected_revision_id=current_revision_id,
                        expected_participant_version=requester.storage_version,
                        expected_workspace_version=workspace_version,
                    )
                    refresh_project_data_cache(project_id)
            except (StorageError, StorageConflictError) as error:
                st.error(str(error))
            else:
                st.success(
                    "変更依頼を保存しました。"
                    "公開版は変更されていません。"
                )
                st.rerun()

def render_schedule_amendments(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None,
    *,
    operations: AmendmentWorkspaceServices,
) -> None:
    st.header("確定日程の改訂")
    if not confirmed:
        st.info("公開中の日程がありません。先に候補を確定・公開してください。")
        return
    current_revision_id = str(
        confirmed.get("schedule_revision", {}).get("id", "")
    )
    public_version = max(1, int(confirmed.get("publication_number", 1)))
    st.success(
        f"参加者には公開版v{public_version}を表示中です。"
        "改訂案を公開するまで、この日程は変わりません。"
    )

    dm_tab, requests_tab, proposals_tab = st.tabs(
        ["DMテンプレート", "変更依頼", "改訂案を作る"],
        key=f"schedule_amendment_tabs_{project_id}",
        on_change="rerun",
    )
    workspace = load_schedule_amendment_workspace(project_id)
    amendment = active_amendment(workspace)
    workspace_version = int(workspace.get("_storage_version", 0))

    if dm_tab.open:
        render_amendment_dm_template_tab(project_id, config)
        return
    if requests_tab.open:
        render_amendment_requests_tab(
            project_id,
            config,
            participants,
            amendment,
            current_revision_id=current_revision_id,
            public_version=public_version,
            workspace_version=workspace_version,
            operations=operations,
        )
        return
    if not proposals_tab.open:
        return
    if amendment is None or not amendment_requests(amendment):
        st.info(
            "変更箇所がありません。"
            "先に「変更依頼」タブで変更内容を登録してください。"
        )
        return

    participant_names = {
        participant.id: participant.name for participant in participants
    }
    base_revision_id = (
        str(amendment.get("base_revision_id", ""))
        or current_revision_id
    )
    amendment_id = str(amendment["id"])

    creation_method = st.segmented_control(
        "改訂案の作り方",
        ["変更最小化探索", "完全手動調整"],
        default="変更最小化探索",
        key=f"amendment_creation_method_{amendment_id}",
        help=(
            "変更最小化探索は登録済み依頼を満たす案を作ります。"
            "完全手動調整は公開版をそのまま編集して、"
            "非公開の改訂案として保存します。"
        ),
    )
    # An amendment starts from the published schedule.  It must remain usable
    # when later response collection is incomplete; only a changed public
    # revision makes the active amendment stale.
    search_disabled = base_revision_id != current_revision_id
    if creation_method == "変更最小化探索":
        st.caption(
            "変更依頼者以外の日時変更数が最小となる改訂案を探索し、"
            "変更最小と確認できた時点、または探索時間上限で終了します。"
            "時間切れの場合は、その時点で変更が最も少ない案を使用します。"
        )
        with st.expander("改訂探索の設定"):
            st.caption(
                "探索時間と再現用シードは通常の候補探索と共通です。"
                "探索案数と日時変更上限は改訂探索だけに使用します。"
            )
            with st.form(
                f"amendment_search_settings_{project_id}",
                enter_to_submit=False,
            ):
                setting_columns = st.columns([1, 1, 1, 1])
                amendment_search_timeout = int(
                    setting_columns[0].number_input(
                        "探索時間上限（秒）",
                        1,
                        operations.max_search_seconds,
                        min(
                            config.search_timeout_seconds,
                            operations.max_search_seconds,
                        ),
                        key=f"amendment_search_timeout_{project_id}",
                        help=(
                            "3案すべてを探すために使う合計時間です。"
                        ),
                    )
                )
                amendment_search_seed = int(
                    setting_columns[1].number_input(
                        "再現用シード",
                        0,
                        999999,
                        config.random_seed,
                        key=f"amendment_search_seed_{project_id}",
                        help=(
                            "値を変えると別の改訂案を"
                            "見つけやすくなります。"
                        ),
                    )
                )
                amendment_candidate_count = int(
                    setting_columns[2].number_input(
                        "探索案数",
                        1,
                        3,
                        min(
                            3,
                            max(1, config.amendment_candidate_count),
                        ),
                        key=f"amendment_candidate_count_{project_id}",
                        help=(
                            "少ないほど最初の案を早く"
                            "見つけやすくなります。"
                        ),
                    )
                )
                amendment_max_non_requester_changes = int(
                    setting_columns[3].number_input(
                        "依頼者以外の日時変更上限（人）",
                        min_value=0,
                        value=max(
                            0,
                            config.amendment_max_non_requester_changes,
                        ),
                        step=1,
                        key=(
                            "amendment_max_non_requester_changes_"
                            f"{project_id}"
                        ),
                        help=(
                            "0人からこの人数まで1人ずつ"
                            "上限を広げて探索します。"
                        ),
                    )
                )
                save_amendment_search_settings = st.form_submit_button(
                    "探索設定を保存"
                )
        if save_amendment_search_settings:
            updates = {
                "search_timeout_seconds": amendment_search_timeout,
                "random_seed": amendment_search_seed,
                "amendment_candidate_count": amendment_candidate_count,
                "amendment_max_non_requester_changes": (
                    amendment_max_non_requester_changes
                ),
            }
            changed = operations.changed_config_updates(config, updates)
            try:
                with status_message("改訂探索の設定を保存しています..."):
                    version = save_config_fields(
                        project_id,
                        updates,
                        expected_version=getattr(
                            config,
                            "_storage_version",
                            None,
                        ),
                    )
                    update_cached_config(
                        project_id,
                        updates,
                        storage_version=version,
                    )
            except (StorageError, StorageConflictError) as error:
                st.error(str(error))
            else:
                if changed:
                    st.success("改訂探索の設定を保存しました。")
                else:
                    st.info("保存する変更はありません。")
                st.rerun()

        if st.button(
            f"改訂案を最大{config.amendment_candidate_count}案探索",
            type="primary",
            disabled=search_disabled,
            key=f"search_schedule_amendment_{amendment_id}",
        ):
            search_owner = st.session_state.setdefault(
                "search_owner_id",
                uuid4().hex,
            )
            if not acquire_job_lock(
                project_id,
                search_owner,
                config.search_timeout_seconds + 30,
            ):
                st.warning("この企画では別の探索が実行中です。")
            else:
                proposals: list[dict] = []
                reasons: list[str] = []
                proposals_saved = False
                try:
                    with st.spinner(
                        f"最大{config.search_timeout_seconds}秒で"
                        f"改訂案を最大{config.amendment_candidate_count}案"
                        "探索しています..."
                    ):
                        target_ids = [
                            participant.id
                            for participant
                            in operations.scheduling_target_participants(
                                participants
                            )
                        ]
                        blocked = load_cross_project_blocked_slots(
                            project_id,
                            target_ids,
                        )
                        unavailable_by_participant = (
                            amendment_unavailable_slots_by_participant(
                                amendment
                            )
                        )
                        proposals, reasons = operations.scheduler_tools()[
                            "generate_amendment_candidates"
                        ](
                            config,
                            participants,
                            confirmed,
                            set(unavailable_by_participant),
                            unavailable_by_participant,
                            timeout_seconds=config.search_timeout_seconds,
                            random_seed=config.random_seed,
                            candidate_limit=config.amendment_candidate_count,
                            blocked_slots_by_participant=blocked,
                        )
                        if proposals:
                            save_schedule_amendment_drafts(
                                project_id,
                                proposals,
                                amendment_id=amendment_id,
                                source="amendment_proposal",
                                expected_revision_id=current_revision_id,
                                expected_workspace_version=workspace_version,
                                replace_proposals=True,
                            )
                            proposals_saved = True
                except (StorageError, StorageConflictError) as error:
                    st.error(str(error))
                finally:
                    release_job_lock(project_id, search_owner)
                if proposals_saved:
                    st.success(
                        f"非公開の改訂案を{len(proposals)}案保存しました。"
                        "公開版は変更されていません。"
                    )
                    for reason in reasons:
                        st.info(reason)
                    st.rerun()
                elif not proposals:
                    for reason in reasons or [
                        "変更依頼を満たす改訂案が"
                        "見つかりませんでした。"
                    ]:
                        st.warning(reason)
    else:
        st.caption(
            "公開版を出発点に、日時・参加者・役割・開催形式を"
            "すべて手動で編集します。公開版は上書きされず、"
            "保存結果は新しい非公開案になります。"
        )
        manual_initial_sessions = schedule_calendar_initial_sessions(
            confirmed,
            participants,
            lock_sessions=False,
            lock_meeting_modes=False,
            lock_members=False,
            reset_roles=False,
        )
        manual_sessions = operations.schedule_calendar_editor(
            calendar_dates_for_editing(config, manual_initial_sessions),
            periods=config.enabled_periods,
            sessions=manual_initial_sessions,
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
            key=f"full_manual_amendment_{amendment_id}",
            show_optimization_controls=False,
            excluded_dates=config.excluded_dates,
        )
        full_manual_confirmation_token = export_cache_token(
            {
                "sessions": manual_sessions,
                "config": config.to_dict(),
                "participants": [
                    participant.to_dict() for participant in participants
                ],
            }
        )
        allow_full_manual_warnings = st.checkbox(
            "設定・参加可能回答との差異を確認し、"
            "警告があっても改訂案として保存する",
            key=(
                f"full_manual_amendment_override_{amendment_id}_"
                f"{full_manual_confirmation_token}"
            ),
        )
        if st.button(
            "完全手動の内容を非公開案として保存",
            type="primary",
            disabled=base_revision_id != current_revision_id,
            key=f"save_full_manual_amendment_{amendment_id}",
        ):
            manual_proposal, errors, policy_issues = (
                candidate_from_calendar_sessions(
                    confirmed,
                    manual_sessions,
                    config,
                    participants,
                    origin={
                        "kind": "manual_amendment_from_public",
                        "source_revision_id": current_revision_id,
                    },
                )
            )
            unavailable_by_participant = (
                amendment_unavailable_slots_by_participant(amendment)
            )
            if manual_proposal is not None:
                for session in manual_proposal.get("sessions", []):
                    slot_key = make_slot_key(
                        str(session.get("date", "")),
                        int(session.get("period", 0)),
                    )
                    member_ids = {
                        *map(
                            str,
                            session.get(
                                "university_role_member_ids",
                                [],
                            ),
                        ),
                        *map(
                            str,
                            session.get(
                                "high_school_role_member_ids",
                                [],
                            ),
                        ),
                    }
                    for participant_id, blocked_slots in (
                        unavailable_by_participant.items()
                    ):
                        if (
                            participant_id in member_ids
                            and slot_key in blocked_slots
                        ):
                            errors.append(
                                f"{participant_names.get(participant_id, participant_id)}"
                                f"さんを、変更依頼で不可能とした"
                                f"{format_slot(slot_key)}から外してください。"
                            )
            for error in list(dict.fromkeys(errors)):
                st.error(error)
            if policy_issues and not allow_full_manual_warnings:
                for issue in policy_issues:
                    st.warning(issue)
                st.info(
                    "内容が意図どおりなら確認欄を選び、"
                    "もう一度保存してください。"
                )
            elif manual_proposal is not None and not errors:
                try:
                    with status_message("完全手動の改訂案を保存しています..."):
                        save_schedule_amendment_drafts(
                            project_id,
                            [manual_proposal],
                            amendment_id=amendment_id,
                            source="manual_amendment",
                            expected_revision_id=current_revision_id,
                            expected_workspace_version=workspace_version,
                        )
                except (StorageError, StorageConflictError) as error:
                    st.error(str(error))
                else:
                    st.success(
                        "完全手動の内容を新しい非公開案として"
                        "保存しました。"
                    )
                    st.rerun()

    proposal_ids = [
        str(value)
        for value in amendment.get("proposal_revision_ids", [])
    ]
    proposal_by_id = {
        revision_id: proposal
        for revision_id in proposal_ids
        if (
            proposal := load_schedule_revision(project_id, revision_id)
        )
        is not None
    }
    if not proposal_by_id:
        st.info("改訂案はまだありません。")
        return

    st.subheader("3. 改訂案を比較・手動調整")
    summary_rows = []
    requester_id_set = amendment_requester_ids(amendment)
    amendment_stage_labels = {
        "direct_swap": "直接入替",
        "seat_repair": "公開済みコマ内で再配置",
        "session_move": "変更コマをまとめて移動",
        "scoped_session_repair": "影響コマ＋別の1コマを再構成",
        "local_repair": "変更周辺から探索",
        "all_participants": "全体から探索",
        "requesters_only": "依頼者のみ",
        "affected_sessions": "変更コマ周辺",
    }
    for position, (revision_id, proposal) in enumerate(
        proposal_by_id.items(),
        start=1,
    ):
        metrics = proposal.get("metrics", {})
        source = str(proposal.get("schedule_revision", {}).get("source", ""))
        origin_kind = str(proposal.get("origin", {}).get("kind", ""))
        summary_rows.append(
            {
                "案": position,
                "作成方法": (
                    "探索案"
                    if source == "amendment_proposal"
                    else (
                        "完全手動案"
                        if origin_kind
                        == "manual_amendment_from_public"
                        else "手動調整案"
                    )
                ),
                "依頼者以外の日時変更人数": metrics.get(
                    "amendment_non_requester_changed_count",
                    sum(
                        1
                        for row in participant_schedule_changes(
                            confirmed,
                            proposal,
                            participant_names,
                        )
                        if row["dm_required"]
                        and row["participant_id"] not in requester_id_set
                    ),
                ),
                "探索段階": amendment_stage_labels.get(
                    str(metrics.get("amendment_search_stage", "")),
                    (
                        "完全手動"
                        if origin_kind
                        == "manual_amendment_from_public"
                        else "手動"
                    ),
                ),
                "最小性": (
                    "公開済みコマ内で変更最小を確認済み"
                    if (
                        source == "amendment_proposal"
                        and not metrics.get(
                            "amendment_optimization_incomplete",
                            True,
                        )
                    )
                    else (
                        "時間内で最小の案"
                        if source == "amendment_proposal"
                        else "-"
                    )
                ),
                "探索時間（秒）": metrics.get(
                    "amendment_total_search_elapsed_seconds",
                    "-",
                ),
                "revision_id": revision_id,
            }
        )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    key: value
                    for key, value in row.items()
                    if key != "revision_id"
                }
                for row in summary_rows
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    detail_revision_id = st.selectbox(
        "詳細表示する改訂案",
        list(proposal_by_id),
        format_func=lambda value: (
            f"案{next(row['案'] for row in summary_rows if row['revision_id'] == value)} / "
            f"{next(row['作成方法'] for row in summary_rows if row['revision_id'] == value)}"
        ),
        key=f"amendment_detail_{amendment_id}",
    )
    detail_proposal = proposal_by_id[detail_revision_id]
    change_rows = amendment_change_rows(
        confirmed,
        detail_proposal,
        participant_names,
    )
    if change_rows:
        st.dataframe(
            pd.DataFrame(change_rows),
            hide_index=True,
            width="stretch",
        )
    else:
        st.info("公開版からの配置変更はありません。")
    with st.expander("この案の日程全体を確認"):
        operations.show_candidate(
            project_id,
            config,
            detail_proposal,
            next(
                row["案"]
                for row in summary_rows
                if row["revision_id"] == detail_revision_id
            ),
            confirmable=False,
            allow_download=False,
            participants=participants,
        )
    with st.expander("この案をさらに手動で調整"):
        st.caption(
            "元の改訂案は残したまま、調整結果を新しい非公開の改訂案として保存します。"
        )
        edited_initial_sessions = schedule_calendar_initial_sessions(
            detail_proposal,
            participants,
            lock_sessions=False,
            lock_meeting_modes=False,
            lock_members=False,
            reset_roles=False,
        )
        edited_sessions = operations.schedule_calendar_editor(
            calendar_dates_for_editing(config, edited_initial_sessions),
            periods=config.enabled_periods,
            sessions=edited_initial_sessions,
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
            key=f"manual_amendment_{amendment_id}_{detail_revision_id}",
            show_optimization_controls=False,
            excluded_dates=config.excluded_dates,
        )
        manual_confirmation_token = export_cache_token(
            {
                "sessions": edited_sessions,
                "config": config.to_dict(),
                "participants": [
                    participant.to_dict() for participant in participants
                ],
            }
        )
        allow_manual_warnings = st.checkbox(
            "設定・参加可能回答との差異を確認し、警告があっても改訂案として保存する",
            key=(
                f"manual_amendment_override_{detail_revision_id}_"
                f"{manual_confirmation_token}"
            ),
        )
        if st.button(
            "手動調整内容を別の改訂案として保存",
            key=f"save_manual_amendment_{detail_revision_id}",
        ):
            manual_proposal, errors, policy_issues = (
                candidate_from_calendar_sessions(
                    detail_proposal,
                    edited_sessions,
                    config,
                    participants,
                    origin={
                        "kind": "manual_amendment",
                        "source_revision_id": detail_revision_id,
                    },
                )
            )
            for error in errors:
                st.error(error)
            if policy_issues and not allow_manual_warnings:
                for issue in policy_issues:
                    st.warning(issue)
                st.info(
                    "内容が意図どおりなら確認欄を選び、もう一度保存してください。"
                )
            elif manual_proposal is not None and not errors:
                try:
                    with status_message("手動調整した改訂案を保存しています..."):
                        save_schedule_amendment_drafts(
                            project_id,
                            [manual_proposal],
                            amendment_id=amendment_id,
                            source="manual_amendment",
                            source_revision_id=detail_revision_id,
                            expected_revision_id=current_revision_id,
                            expected_workspace_version=workspace_version,
                        )
                except (StorageError, StorageConflictError) as error:
                    st.error(str(error))
                else:
                    st.success(
                        "手動調整結果を新しい非公開の改訂案として保存しました。"
                    )
                    st.rerun()

    st.subheader("4. DM作成・返信メモ")
    selected_ids = [
        str(value)
        for value in amendment.get("selected_revision_ids", [])
        if str(value) in proposal_by_id
    ]
    dm_revision_ids = st.multiselect(
        "DMにまとめる改訂案（最大3案）",
        list(proposal_by_id),
        default=selected_ids,
        max_selections=3,
        format_func=lambda value: (
            f"案{next(row['案'] for row in summary_rows if row['revision_id'] == value)} / "
            f"{next(row['作成方法'] for row in summary_rows if row['revision_id'] == value)}"
        ),
        key=f"amendment_dm_proposals_{amendment_id}",
    )
    if dm_revision_ids != selected_ids and st.button(
        "DM比較対象を保存",
        key=f"save_amendment_dm_selection_{amendment_id}",
    ):
        try:
            with status_message("DM比較対象を保存しています..."):
                select_schedule_amendment_proposals(
                    project_id,
                    amendment_id,
                    dm_revision_ids,
                    expected_workspace_version=workspace_version,
                )
        except (StorageError, StorageConflictError) as error:
            st.error(str(error))
        else:
            st.success("DMにまとめる改訂案を保存しました。")
            st.rerun()
    messages = build_dm_messages(
        confirmed,
        [proposal_by_id[value] for value in dm_revision_ids],
        participant_names,
        prefix=config.amendment_dm_template_prefix,
        suffix=config.amendment_dm_template_suffix,
    )
    if not messages:
        st.info("日時・開催形式が変わる参加者はいません。")
    status_options = ["unanswered", "possible", "impossible"]
    status_labels = {
        "unanswered": "未回答",
        "possible": "可能",
        "impossible": "不可能",
    }
    selected_token = hashlib.sha256(
        "|".join(dm_revision_ids).encode("utf-8")
    ).hexdigest()[:10]
    for message in messages:
        participant_id = str(message["participant_id"])
        with st.container(border=True):
            st.markdown(f"#### {message['participant_name']}さん")
            st.text_area(
                "DM文面",
                value=str(message["message"]),
                height=180,
                key=(
                    f"amendment_dm_text_{amendment_id}_"
                    f"{participant_id}_{selected_token}"
                ),
            )
            participant_replies = amendment.get("reply_memos", {}).get(
                participant_id,
                {},
            )
            for option in message["proposed_options"]:
                reply = participant_replies.get(option, {})
                current_status = str(
                    reply.get("status", "unanswered")
                )
                if current_status not in status_options:
                    current_status = "unanswered"
                columns = st.columns([2, 2, 3, 1])
                columns[0].write(format_dm_option(option))
                response_status = columns[1].selectbox(
                    "返信",
                    status_options,
                    index=status_options.index(current_status),
                    format_func=lambda value: status_labels[value],
                    key=(
                        f"amendment_reply_status_{amendment_id}_"
                        f"{participant_id}_{option}"
                    ),
                    label_visibility="collapsed",
                )
                reply_note = columns[2].text_input(
                    "メモ",
                    value=str(reply.get("note", "")),
                    key=(
                        f"amendment_reply_note_{amendment_id}_"
                        f"{participant_id}_{option}"
                    ),
                    label_visibility="collapsed",
                )
                if columns[3].button(
                    "保存",
                    key=(
                        f"amendment_reply_save_{amendment_id}_"
                        f"{participant_id}_{option}"
                    ),
                ):
                    try:
                        with status_message("返信メモを保存しています..."):
                            save_schedule_amendment_reply(
                                project_id,
                                amendment_id,
                                participant_id,
                                option,
                                status=response_status,
                                note=reply_note,
                                expected_workspace_version=workspace_version,
                            )
                    except (StorageError, StorageConflictError) as error:
                        st.error(str(error))
                    else:
                        st.rerun()

    st.subheader("5. 改訂案を確認して公開")
    publish_revision_id = st.selectbox(
        "公開する改訂案",
        list(proposal_by_id),
        format_func=lambda value: (
            f"案{next(row['案'] for row in summary_rows if row['revision_id'] == value)} / "
            f"{next(row['作成方法'] for row in summary_rows if row['revision_id'] == value)}"
        ),
        key=f"publish_amendment_proposal_{amendment_id}",
    )
    publish_reply_rows = amendment_reply_rows(
        amendment,
        confirmed,
        proposal_by_id[publish_revision_id],
        participant_names,
    )
    st.write(
        "返信状況: "
        f"可能 {sum(row['状態'] == 'possible' for row in publish_reply_rows)}件 / "
        f"不可能 {sum(row['状態'] == 'impossible' for row in publish_reply_rows)}件 / "
        f"未回答 {sum(row['状態'] == 'unanswered' for row in publish_reply_rows)}件"
    )
    if st.button(
        f"確認して公開版v{public_version + 1}へ切り替える",
        type="primary",
        disabled=base_revision_id != current_revision_id,
        key=f"open_publish_amendment_{amendment_id}",
    ):
        config_for_dialog = config.to_dict()
        config_for_dialog["_participants"] = [
            participant.to_dict() for participant in participants
        ]
        schedule_amendment_publish_confirmation_dialog(
            project_id,
            config_for_dialog,
            amendment,
            proposal_by_id[publish_revision_id],
            confirmed,
            current_revision_id,
            workspace_version,
            participant_names,
        )
