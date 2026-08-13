"""Current published-schedule display and revision-history controls."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.domain.models import (
    Config,
    Participant,
    ROLE_DISPLAY_COLORS,
    ROLE_DISPLAY_LABELS,
)
from schedule_adjustment_tool.exports.spreadsheet_exports import (
    confirmed_schedule_workbook,
)
from schedule_adjustment_tool.domain.schedule_model import (
    ScheduleModelError,
    schedule_policy_issues,
)
from schedule_adjustment_tool.domain.scheduler import (
    refresh_candidate_evaluation,
)
from schedule_adjustment_tool.ui.presentation import ROLE_DISPLAY_MODE_CHOICES


ROLE_DISPLAY_LABEL_OPTION, ROLE_DISPLAY_COLOR_OPTION = ROLE_DISPLAY_MODE_CHOICES


@dataclass(frozen=True)
class CurrentScheduleServices:
    """App-level operations invoked from the published-schedule screen."""

    list_schedule_revisions: Callable[[str], list[dict]]
    show_candidate: Callable[..., None]
    format_datetime: Callable[[str], str]
    render_prepared_download: Callable[..., None]
    export_cache_token: Callable[[object], str]
    confirm_revision_restore: Callable[..., None]
    confirm_schedule_clear: Callable[..., None]


def render_schedule_revision_history(
    project_id: str,
    confirmed: dict | None,
    *,
    services: CurrentScheduleServices,
) -> None:
    """Show restorable published revisions without changing them directly."""

    with st.expander("公開履歴・旧版への戻し"):
        revisions = [
            revision
            for revision in services.list_schedule_revisions(project_id)
            if str(revision.get("source", ""))
            not in {"amendment_proposal", "manual_amendment"}
        ]
        if not revisions:
            st.caption("公開履歴はありません。")
            return
        source_labels = {
            "candidate": "候補を公開",
            "manual": "手動登録",
            "edit": "直接編集",
            "reoptimization": "再最適化",
            "restore": "旧版を元に再公開",
            "migration": "以前の確定日程",
            "amendment_publish": "改訂版を公開",
        }
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "履歴番号": revision["revision_number"],
                        "現在公開中": "はい" if revision.get("active") else "",
                        "操作": source_labels.get(
                            str(revision.get("source", "")),
                            str(revision.get("source", "")),
                        ),
                        "メモ": revision.get("change_note", ""),
                        "登録日時": revision.get("created_at", ""),
                    }
                    for revision in revisions
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        restorable = [
            revision for revision in revisions if not revision.get("active")
        ]
        if not restorable:
            return
        by_id = {str(revision["id"]): revision for revision in restorable}
        revision_id = st.selectbox(
            "再公開の元にする旧版",
            list(by_id),
            format_func=lambda value: (
                f"履歴{by_id[value]['revision_number']} / "
                f"{source_labels.get(str(by_id[value]['source']), by_id[value]['source'])}"
            ),
            key=f"public_history_restore_{project_id}",
        )
        if st.button(
            "選択した旧版を新しい公開版として復元",
            key=f"public_history_restore_button_{project_id}",
        ):
            services.confirm_revision_restore(
                project_id,
                "公開日程",
                revision_id,
                int(by_id[revision_id]["revision_number"]),
                str(
                    (confirmed or {})
                    .get("schedule_revision", {})
                    .get("id", "")
                ),
            )


def render_current_schedule_operations(
    project_id: str,
    config: Config,
    participants: list[Participant],
    confirmed: dict | None,
    *,
    services: CurrentScheduleServices,
) -> None:
    """Render the current publication without changing revision semantics."""

    if not confirmed:
        st.info("現在公開されている日程はありません。")
        st.caption(
            "過去に公開した日程がある場合は、公開履歴から旧版を選んで"
            "新しい公開版として復元できます。"
        )
        render_schedule_revision_history(
            project_id,
            None,
            services=services,
        )
        return
    st.success(
        f"公開版v{max(1, int(confirmed.get('publication_number', 1)))} / "
        f"公開日時 {services.format_datetime(confirmed.get('confirmed_at', ''))}"
    )
    try:
        policy_issues = schedule_policy_issues(
            confirmed,
            config,
            participants,
        )
    except ScheduleModelError as error:
        policy_issues = [f"公開日程のデータを確認できません: {error}"]
    if policy_issues:
        st.warning(
            "公開中の日程は、現在の条件・参加者状態との差異があるため要確認です。"
        )
        for issue in policy_issues:
            st.warning(issue)
    evaluation_context: dict[str, object] | None = None
    evaluation_participants: list[Participant] | None = participants
    try:
        current_evaluated_schedule = refresh_candidate_evaluation(
            deepcopy(confirmed),
            config,
            participants,
            evaluation_config=config,
        )
    except KeyError:
        current_evaluated_schedule = deepcopy(confirmed)
        evaluation_participants = None
        st.warning(
            "公開日程に現在の参加者一覧で確認できない人が含まれるため、"
            "現在の評価設定では適合度を再計算できません。"
        )
    else:
        evaluation_context = {
            "selected_label": "現在の評価設定",
            "stored_label": "公開時の評価設定",
            "recalculated": True,
        }
    services.show_candidate(
        project_id,
        config,
        current_evaluated_schedule,
        int(confirmed.get("candidate_number", 1)),
        confirmable=False,
        allow_download=False,
        participants=evaluation_participants,
        policy_issues=tuple(policy_issues),
        calendar_first=True,
        evaluation_context=evaluation_context,
    )
    confirmed_display = st.session_state.get(
        (
            f"candidate_role_display_{project_id}_"
            f"{int(confirmed.get('candidate_number', 1))}_confirmed"
        ),
        (
            ROLE_DISPLAY_COLOR_OPTION
            if config.role_display_mode == ROLE_DISPLAY_COLORS
            else ROLE_DISPLAY_LABEL_OPTION
        ),
    )
    role_display_mode = (
        ROLE_DISPLAY_COLORS
        if confirmed_display == ROLE_DISPLAY_COLOR_OPTION
        else ROLE_DISPLAY_LABELS
    )
    services.render_prepared_download(
        st,
        project_id=project_id,
        kind="confirmed_schedule",
        cache_token=services.export_cache_token(
            {
                "config": config.to_dict(),
                "confirmed": current_evaluated_schedule,
                "participants": [
                    participant.to_dict() for participant in participants
                ],
                "role_display_mode": role_display_mode,
            }
        ),
        prepare_label="確定日程Excelを準備",
        download_label="確定日程をExcel出力",
        status_label="確定日程のExcelを準備しています...",
        build=confirmed_schedule_workbook,
        build_args=(
            config,
            current_evaluated_schedule,
            participants,
            role_display_mode,
        ),
        file_name=f"{config.title}_確定日程.xlsx",
        audit_action="confirmed_schedule.exported",
    )
    st.warning(
        "確定を取り消すと、参加者には公開日程が表示されなくなります。"
    )
    if st.button(
        "確定を取り消して再調整する",
        key=f"focused_clear_confirmed_{project_id}",
    ):
        services.confirm_schedule_clear(
            project_id,
            config.title,
            str(confirmed.get("schedule_revision", {}).get("id", "")),
        )
    render_schedule_revision_history(
        project_id,
        confirmed,
        services=services,
    )
