"""Supporting UI for post-publication schedule amendments.

The amendment workspace, revisions, and CAS writes remain in storage. This
module only owns amendment-specific presentation and confirmation controls.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.domain.amendments import (
    format_dm_option,
    participant_schedule_changes,
)
from schedule_adjustment_tool.domain.models import (
    Config,
    Participant,
    WEEKDAY_LABELS,
    eligible_dates,
    make_slot_key,
)
from schedule_adjustment_tool.domain.schedule_model import (
    ScheduleModelError,
    schedule_policy_issues,
)
from schedule_adjustment_tool.storage import (
    StorageConflictError,
    StorageError,
    discard_schedule_amendment,
    publish_schedule_amendment_draft,
    save_config_fields,
)
from schedule_adjustment_tool.ui.manager.app_cache import (
    clear_prepared_exports,
    status_message,
)
from schedule_adjustment_tool.ui.manager.project_cache import (
    refresh_project_data_cache,
    set_cached_confirmed,
    update_cached_config,
)

def amendment_change_rows(
    base_schedule: dict,
    proposal: dict,
    participant_names: dict[str, str],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    role_labels = {
        "university": "大学生役",
        "high_school": "高校生役",
    }
    for change in participant_schedule_changes(
        base_schedule,
        proposal,
        participant_names,
    ):
        role_changes = []
        for role_change in change["role_changes"]:
            before = "・".join(
                role_labels.get(role, role)
                for role in role_change["before_roles"]
            )
            after = "・".join(
                role_labels.get(role, role)
                for role in role_change["after_roles"]
            )
            role_changes.append(
                f"{format_dm_option(role_change['option_key'])}: "
                f"{before}→{after}"
            )
        rows.append(
            {
                "参加者": str(change["participant_name"]),
                "現在": (
                    "／".join(
                        format_dm_option(value)
                        for value in change["current_options"]
                    )
                    or "日時変更なし"
                ),
                "変更後": (
                    "／".join(
                        format_dm_option(value)
                        for value in change["proposed_options"]
                    )
                    or "日時変更なし"
                ),
                "内部の役割変更": "／".join(role_changes) or "なし",
            }
        )
    return rows


def amendment_reply_rows(
    amendment: dict,
    base_schedule: dict,
    proposal: dict,
    participant_names: dict[str, str],
) -> list[dict[str, str]]:
    status_labels = {
        "possible": "可能",
        "impossible": "不可能",
        "unanswered": "未回答",
    }
    reply_memos = amendment.get("reply_memos", {})
    rows: list[dict[str, str]] = []
    for change in participant_schedule_changes(
        base_schedule,
        proposal,
        participant_names,
    ):
        if not change["dm_required"]:
            continue
        participant_id = str(change["participant_id"])
        participant_replies = reply_memos.get(participant_id, {})
        options = change["proposed_options"] or [""]
        for option in options:
            reply = participant_replies.get(option, {})
            status = str(reply.get("status", "unanswered"))
            rows.append(
                {
                    "参加者": str(change["participant_name"]),
                    "変更後": (
                        format_dm_option(option)
                        if option
                        else "日程から外れる"
                    ),
                    "返信": status_labels.get(status, "未回答"),
                    "状態": status if status in status_labels else "unanswered",
                    "メモ": str(reply.get("note", "")),
                }
            )
    return rows


@st.dialog("改訂案を公開")
def schedule_amendment_publish_confirmation_dialog(
    project_id: str,
    config_payload: dict,
    amendment_payload: dict,
    proposal: dict,
    base_schedule: dict,
    expected_revision_id: str,
    expected_workspace_version: int,
    participant_names: dict[str, str],
) -> None:
    config = Config.from_dict(config_payload)
    revision_id = str(
        proposal.get("schedule_revision", {}).get("id", "")
    )
    reply_rows = amendment_reply_rows(
        amendment_payload,
        base_schedule,
        proposal,
        participant_names,
    )
    pending_rows = [
        row for row in reply_rows if row["状態"] != "possible"
    ]
    st.write(
        f"公開版v{int(base_schedule.get('publication_number', 1))}を"
        f"公開版v{int(base_schedule.get('publication_number', 1)) + 1}"
        "へ切り替えます。現在の公開版は履歴に残ります。"
    )
    if pending_rows:
        st.warning(
            "不可能または未回答の変更があります。"
            "内容を確認したうえで公開できます。"
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "参加者": row["参加者"],
                        "変更後": row["変更後"],
                        "返信": row["返信"],
                        "メモ": row["メモ"],
                    }
                    for row in pending_rows
                ]
            ),
            hide_index=True,
            width="stretch",
        )
    try:
        policy_issues = schedule_policy_issues(
            proposal,
            config,
            [
                Participant.from_dict(value)
                for value in config_payload.get("_participants", [])
                if isinstance(value, dict)
            ],
        )
    except ScheduleModelError as error:
        policy_issues = [str(error)]
    if policy_issues:
        st.warning("現在の設定・参加可能回答との差異があります。")
        for issue in policy_issues:
            st.write(f"- {issue}")
    acknowledgement_required = bool(pending_rows or policy_issues)
    acknowledged = (
        st.checkbox(
            "不可能・未回答・警告の内容を確認しました",
            key=f"publish_amendment_ack_{revision_id}",
        )
        if acknowledgement_required
        else True
    )
    change_note = st.text_input(
        "公開時のメモ",
        value=str(amendment_payload.get("reason", "")),
        max_chars=1000,
        key=f"publish_amendment_note_{revision_id}",
    )
    columns = st.columns(2)
    if columns[0].button(
        "新しい公開版として確定・公開",
        type="primary",
        disabled=not acknowledged,
        key=f"publish_amendment_confirm_{revision_id}",
    ):
        try:
            with status_message("改訂案を新しい公開版として保存しています..."):
                result = publish_schedule_amendment_draft(
                    project_id,
                    revision_id,
                    amendment_id=str(amendment_payload["id"]),
                    expected_revision_id=expected_revision_id,
                    expected_workspace_version=expected_workspace_version,
                    change_note=change_note,
                )
                set_cached_confirmed(project_id, result["schedule"])
                refresh_project_data_cache(project_id)
                clear_prepared_exports(project_id)
        except (StorageError, StorageConflictError) as error:
            st.error(str(error))
        else:
            st.success("新しい公開版へ切り替えました。")
            st.rerun()
    if columns[1].button(
        "戻る",
        key=f"publish_amendment_cancel_{revision_id}",
    ):
        st.rerun()

def render_amendment_unavailable_calendar(
    project_id: str,
    config: Config,
    participant: Participant,
    *,
    amendment_token: str,
    already_requested_slots: set[str] | None = None,
) -> list[str]:
    registered_possible_slots = (
        set(participant.availability)
        | set(participant.zoom_availability)
    )
    already_requested_slots = set(already_requested_slots or set())
    possible_slots = registered_possible_slots - already_requested_slots

    visible_days = [
        day
        for day in eligible_dates(config)
        if any(
            make_slot_key(day, period) in possible_slots
            for period in config.enabled_periods
        )
    ]
    if not visible_days:
        if registered_possible_slots and already_requested_slots:
            st.info(
                f"{participant.name}さんには、この改訂作業へ追加できる"
                "参加可能コマが残っていません。"
            )
        else:
            st.info(
                f"{participant.name}さんには、現在参加可能として登録されている"
                "コマがありません。"
            )
        return []

    st.caption(
        "本人入力または「入力状況」での代理入力は変更せず、"
        "この改訂作業だけで不可能として扱います。"
        "現在参加可能で、この改訂に未登録のコマだけ選択できます。"
        "「—」はもともと参加不可能なため選択対象外です。"
    )
    column_widths = [1.5, *([1.0] * len(config.enabled_periods))]
    header_columns = st.columns(column_widths)
    header_columns[0].markdown("**日付**")
    for column, period in zip(
        header_columns[1:],
        config.enabled_periods,
    ):
        column.markdown(f"**{period}限**")

    selected_slots: list[str] = []
    for day in visible_days:
        row_columns = st.columns(column_widths)
        row_columns[0].markdown(
            f"**{day.month}/{day.day}"
            f"（{WEEKDAY_LABELS[day.weekday()]}）**"
        )
        for column, period in zip(
            row_columns[1:],
            config.enabled_periods,
        ):
            slot_key = make_slot_key(day, period)
            if slot_key not in possible_slots:
                column.caption("—")
                continue
            selected = column.checkbox(
                f"{day.isoformat()} {period}限を不可能にする",
                key=(
                    f"amendment_unavailable_{project_id}_"
                    f"{amendment_token}_{participant.id}_{slot_key}"
                ),
                label_visibility="collapsed",
                help=(
                    f"{day.isoformat()} "
                    f"（{WEEKDAY_LABELS[day.weekday()]}）"
                    f"{period}限を不可能として登録"
                ),
            )
            if selected:
                selected_slots.append(slot_key)
    return selected_slots


def render_amendment_dm_template_tab(
    project_id: str,
    config: Config,
) -> None:
    st.caption(
        "企画ごとに、固定の「現在」「変更後」行の前後を編集できます。"
        "生成するすべてのDMに適用されます。"
    )
    with st.form(f"amendment_dm_template_{project_id}"):
        prefix = st.text_area(
            "冒頭の自由入力",
            value=config.amendment_dm_template_prefix,
            height=100,
        )
        st.code(
            "現在：7月20日（月）2限・対面\n"
            "変更後：7月22日（水）3限・対面／"
            "7月23日（木）3限・対面／7月24日（金）3限・対面"
        )
        suffix = st.text_area(
            "末尾の自由入力",
            value=config.amendment_dm_template_suffix,
            height=80,
        )
        save_template = st.form_submit_button("DMテンプレートを保存")
    if not save_template:
        return
    try:
        with status_message("DMテンプレートを保存しています..."):
            version = save_config_fields(
                project_id,
                {
                    "amendment_dm_template_prefix": prefix,
                    "amendment_dm_template_suffix": suffix,
                },
                expected_version=getattr(config, "_storage_version", None),
            )
            update_cached_config(
                project_id,
                {
                    "amendment_dm_template_prefix": prefix,
                    "amendment_dm_template_suffix": suffix,
                },
                storage_version=version,
            )
    except (StorageError, StorageConflictError) as error:
        st.error(str(error))
    else:
        st.success("この企画のDMテンプレートを保存しました。")
        st.rerun()


@st.dialog("改訂作業を破棄")
def schedule_amendment_discard_confirmation_dialog(
    project_id: str,
    amendment_id: str,
    expected_workspace_version: int,
) -> None:
    st.warning(
        "改訂案と返信メモを現在の作業対象から外します。"
        "この改訂作業だけに登録した不可能コマも取り消します。"
        "本人の入力と「入力状況」で保存した代理入力は変更しません。"
        "公開中の日程は変更されません。保存済みrevisionは監査履歴に残ります。"
    )
    columns = st.columns(2)
    if columns[0].button(
        "改訂作業を破棄",
        type="primary",
        key=f"discard_amendment_confirm_{amendment_id}",
    ):
        try:
            with status_message("改訂作業を破棄しています..."):
                discard_schedule_amendment(
                    project_id,
                    amendment_id,
                    expected_workspace_version=expected_workspace_version,
                )
        except (StorageError, StorageConflictError) as error:
            st.error(str(error))
        else:
            st.success(
                "改訂作業と、この改訂専用の不可能コマを破棄しました。"
                "公開版と参加者回答は変更されていません。"
            )
            st.rerun()
    if columns[1].button(
        "戻る",
        key=f"discard_amendment_cancel_{amendment_id}",
    ):
        st.rerun()
