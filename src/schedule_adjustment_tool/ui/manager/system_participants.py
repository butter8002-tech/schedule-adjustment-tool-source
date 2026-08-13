"""System-wide participant directory administration."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import logging
import time

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.domain.app_config import load_app_settings
from schedule_adjustment_tool.domain.models import participant_name_identity_key
from schedule_adjustment_tool.domain.participant_attributes import DEPARTMENT_OPTIONS
from schedule_adjustment_tool.storage import (
    StorageConflictError,
    StorageError,
    list_project_ids_for_participants,
    update_common_participants,
)
from schedule_adjustment_tool.ui.manager.app_cache import (
    clear_audit_logs_cache,
    clear_common_participants_cache,
    load_common_participants_cached,
    load_system_settings_cached,
    set_cached_common_participants,
    status_message,
)
from schedule_adjustment_tool.ui.manager.participants import (
    common_participant_cohort_label,
    filter_common_participants,
)
from schedule_adjustment_tool.ui.manager.project_cache import clear_project_data_cache
from schedule_adjustment_tool.ui.manager.system_callbacks import (
    common_participant_delete_confirmation_dialog,
    common_participant_table_updates,
    format_datetime_with_weekday,
)


APP_SETTINGS = load_app_settings()
LOGGER = logging.getLogger("schedule_adjustment_tool")


def render_system_participants_section() -> None:
    """Render the shared directory, separate from per-project assignments."""

    rerender_started_at = st.session_state.pop(
        "system_participant_save_rerender_started_at", None
    )
    if isinstance(rerender_started_at, (float, int)):
        rerender_elapsed = time.perf_counter() - float(rerender_started_at)
        if rerender_elapsed >= 0:
            LOGGER.info(
                "common_participant_bulk_save_timing rerender_seconds=%.4f",
                rerender_elapsed,
            )
    st.subheader("登録済み参加者")
    st.caption(
        "企画をまたいで利用する参加者名簿です。"
        "ここで変更した氏名・期・所属情報は、その参加者が登録されている"
        "すべての企画に反映されます。"
    )
    heading_columns = st.columns([4, 1])
    search_text = heading_columns[0].text_input(
        "参加者を検索",
        placeholder="名前の一部を入力",
        key="system_participant_search",
    )
    if heading_columns[1].button(
        "一覧を再読み込み",
        key="refresh_system_participants",
    ):
        load_common_participants_cached(force=True)
        st.rerun()

    profiles = load_common_participants_cached()
    if not profiles:
        st.info(
            "登録済み参加者はまだいません。"
            "企画の参加者管理から追加すると、ここにも表示されます。"
        )
        return

    cohort_labels = sorted(
        {common_participant_cohort_label(profile) for profile in profiles},
        key=lambda label: (
            label == "期未登録",
            int(label.removesuffix("期")) if label.endswith("期") else 9999,
        ),
    )
    cohort_filter = st.segmented_control(
        "期で絞り込み",
        ["すべて", *cohort_labels],
        default="すべて",
        key="system_participant_cohort_filter",
    )
    filtered_profiles = filter_common_participants(
        profiles,
        cohort_label=cohort_filter or "すべて",
        search_text=search_text,
    )
    if not filtered_profiles:
        st.info("この条件に一致する参加者はいません。")
        return

    st.caption(
        "日程調整の対象・班・参加回数は企画ごとに設定します。"
        "この表では、全企画で共通する本人情報を直接変更できます。"
        "編集後に表の下の保存ボタンを押してください。"
    )
    active_cohort_labels = {
        f"{int(cohort)}期"
        for cohort in load_system_settings_cached()["active_cohorts"]
    }
    available_cohort_labels = sorted(
        active_cohort_labels
        | {
            common_participant_cohort_label(profile)
            for profile in profiles
            if common_participant_cohort_label(profile) != "期未登録"
        },
        key=lambda label: int(label.removesuffix("期")),
    )
    rows = [
        {
            "ID": str(profile.get("id", "")),
            "名前": str(profile.get("name", "") or ""),
            "期": common_participant_cohort_label(profile),
            "文理": str(profile.get("humanities_or_science", "") or ""),
            "科類・学部": str(profile.get("department", "") or ""),
            "学科・類・専修": str(profile.get("department_detail", "") or ""),
            "登録企画数": int(profile.get("project_count", 0) or 0),
            "本人アカウント": "連携済み" if profile.get("user_id") else "未連携",
            "最終更新": format_datetime_with_weekday(
                str(profile.get("updated_at", "") or "")
            ),
        }
        for profile in filtered_profiles
    ]
    filter_token = hashlib.sha256(
        "\x1f".join(
            [
                str(cohort_filter or "すべて"),
                participant_name_identity_key(search_text),
                *[str(row["ID"]) for row in rows],
            ]
        ).encode("utf-8")
    ).hexdigest()[:12]
    with st.form(f"system_participant_table_form_{filter_token}"):
        edited_profiles = st.data_editor(
            pd.DataFrame(rows),
            hide_index=True,
            width="stretch",
            height=min(620, 36 * (len(rows) + 1)),
            disabled=["ID", "登録企画数", "本人アカウント", "最終更新"],
            column_config={
                "ID": None,
                "名前": st.column_config.TextColumn(
                    "名前", required=True, max_chars=APP_SETTINGS.max_text_length
                ),
                "期": st.column_config.SelectboxColumn(
                    "期", options=["期未登録", *available_cohort_labels], required=True
                ),
                "文理": st.column_config.SelectboxColumn(
                    "文理", options=["", "文系", "理系", "その他"], required=False
                ),
                "科類・学部": st.column_config.SelectboxColumn(
                    "科類・学部", options=DEPARTMENT_OPTIONS, required=False
                ),
                "学科・類・専修": st.column_config.TextColumn(
                    "学科・類・専修",
                    help="科類・学部を変更した場合は、対応する学科・類・専修へ直すか、空欄にしてください。",
                    max_chars=APP_SETTINGS.max_text_length,
                ),
                "登録企画数": st.column_config.NumberColumn("登録企画数", width="small"),
                "本人アカウント": st.column_config.TextColumn(
                    "本人アカウント", width="small"
                ),
            },
            key=f"system_participant_table_{filter_token}",
        )
        save_table_clicked = st.form_submit_button("表の変更を保存", type="primary")

    if save_table_clicked:
        updates, errors = common_participant_table_updates(profiles, edited_profiles)
        for error in errors:
            st.warning(error)
        if not errors and not updates:
            st.info("保存が必要な変更はありません。")
        elif not errors:
            try:
                with status_message("参加者情報を保存しています..."):
                    affected_projects_by_participant = (
                        list_project_ids_for_participants(
                            [str(update["participant_id"]) for update in updates]
                        )
                    )
                    updated_profiles = update_common_participants(updates)
                    cache_started = time.perf_counter()
                    merged_profiles = {
                        str(profile["id"]): profile for profile in profiles
                    }
                    merged_profiles.update(
                        {
                            str(profile["id"]): profile
                            for profile in updated_profiles
                        }
                    )
                    set_cached_common_participants(
                        sorted(
                            merged_profiles.values(),
                            key=lambda profile: (
                                str(profile.get("name", "")).casefold(),
                                str(profile.get("id", "")),
                            ),
                        )
                    )
                    for project_id in sorted(
                        {
                            project_id
                            for project_ids in affected_projects_by_participant.values()
                            for project_id in project_ids
                        }
                    ):
                        clear_project_data_cache(project_id)
                    clear_audit_logs_cache()
                    LOGGER.info(
                        "common_participant_bulk_save_timing cache_seconds=%.4f "
                        "participant_count=%d project_count=%d",
                        time.perf_counter() - cache_started,
                        len(updates),
                        len(
                            {
                                project_id
                                for project_ids in (
                                    affected_projects_by_participant.values()
                                )
                                for project_id in project_ids
                            }
                        ),
                    )
            except (StorageConflictError, StorageError) as error:
                clear_common_participants_cache()
                affected_project_ids = (
                    {
                        project_id
                        for project_ids in affected_projects_by_participant.values()
                        for project_id in project_ids
                    }
                    if "affected_projects_by_participant" in locals()
                    else set()
                )
                for project_id in sorted(affected_project_ids):
                    clear_project_data_cache(project_id)
                clear_audit_logs_cache()
                st.warning(str(error))
            else:
                st.success(f"{len(updates)}人の参加者情報を保存しました。")
                st.session_state["system_participant_save_rerender_started_at"] = (
                    time.perf_counter()
                )
                st.rerun()

    st.divider()
    st.markdown("**登録済み参加者の削除**")
    st.caption("削除する参加者を1人選び、影響範囲を確認してから実行します。")
    profile_by_id = {
        str(profile.get("id", "")): profile for profile in filtered_profiles
    }
    delete_participant_id = st.selectbox(
        "削除する登録済み参加者",
        list(profile_by_id),
        index=None,
        placeholder="参加者を選択",
        format_func=lambda participant_id: (
            f"{profile_by_id[participant_id].get('name', '')}"
            "（登録企画 "
            f"{int(profile_by_id[participant_id].get('project_count', 0) or 0)}件）"
        ),
        key=f"system_common_participant_delete_{filter_token}",
    )
    if st.button(
        "選択した登録済み参加者を削除",
        disabled=not delete_participant_id,
        key=f"open_system_common_participant_delete_{filter_token}",
    ):
        common_participant_delete_confirmation_dialog(
            deepcopy(profile_by_id[str(delete_participant_id)])
        )
