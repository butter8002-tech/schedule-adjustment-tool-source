"""Operator notices for data behavior changed by an application update."""

from __future__ import annotations

from typing import Any

import streamlit as st

from schedule_adjustment_tool.domain.evaluation_config import (
    EVALUATION_SCORE_VERSION,
)
from schedule_adjustment_tool.storage import (
    StorageError,
    acknowledge_support_role_version_notice,
    load_support_role_version_notice,
)
from schedule_adjustment_tool.ui.manager.app_cache import status_message
from schedule_adjustment_tool.ui.manager.candidate_evaluation import (
    LEGACY_EVALUATION_SOURCE,
)


SUPPORT_NOTICE_DISMISSED_KEY = "support_role_version_notice_dismissed"
LEGACY_EVALUATION_NOTICE_SHOWN_KEY = "legacy_evaluation_version_notice_shown"
VERSION_DIALOG_OPENED_KEY = "version_update_dialog_opened_this_run"


def begin_version_update_notice_render() -> None:
    """Reset the one-dialog guard at the beginning of each Streamlit rerun."""

    st.session_state.pop(VERSION_DIALOG_OPENED_KEY, None)


def _reserve_dialog() -> bool:
    if st.session_state.get(VERSION_DIALOG_OPENED_KEY):
        return False
    st.session_state[VERSION_DIALOG_OPENED_KEY] = True
    return True


def _session_project_set(key: str) -> set[str]:
    value = st.session_state.setdefault(key, set())
    if isinstance(value, set):
        return value
    normalized = {str(item) for item in value} if isinstance(value, list) else set()
    st.session_state[key] = normalized
    return normalized


@st.dialog("バージョン更新に伴う参加条件の補正")
def _support_role_update_dialog(project_id: str, affected_count: int) -> None:
    st.info(
        "ver1.0.1で「遊撃」として扱われていた参加者について、"
        "従来どおり役割指定なしになるよう保存データを"
        "補正しました。"
    )
    st.write(f"この企画で補正した参加者: {affected_count}人")
    st.caption(
        "保存済み候補は削除していません。ver1.0.1でも同じ"
        "成立条件で扱われていたため、日程・回答・候補の内容は"
        "変更していません。"
    )
    hide_future = st.checkbox(
        "この注意を今後表示しない",
        key=f"hide_support_role_version_notice_{project_id}",
    )
    if not st.button(
        "確認して閉じる",
        type="primary",
        key=f"close_support_role_version_notice_{project_id}",
    ):
        return
    if hide_future:
        try:
            with status_message("注意表示の確認状態を保存しています..."):
                acknowledged = acknowledge_support_role_version_notice(
                    project_id
                )
        except StorageError as error:
            st.error(str(error))
            return
        if not acknowledged:
            st.error("注意表示の確認状態を保存できませんでした。")
            return
    _session_project_set(SUPPORT_NOTICE_DISMISSED_KEY).add(project_id)
    st.rerun()


def render_support_role_update_notice(project_id: str) -> None:
    """Show the project notice until someone explicitly acknowledges it."""

    if project_id in _session_project_set(SUPPORT_NOTICE_DISMISSED_KEY):
        return
    notice = load_support_role_version_notice(project_id)
    if not notice or not _reserve_dialog():
        return
    _support_role_update_dialog(
        project_id,
        int(notice.get("affected_count", 0)),
    )


def _is_legacy_evaluation_candidate(candidate: dict[str, Any]) -> bool:
    if candidate.get("evaluation_config_source") == LEGACY_EVALUATION_SOURCE:
        return True
    if (
        candidate.get("metrics", {}).get("evaluation_score_version")
        != EVALUATION_SCORE_VERSION
    ):
        return True
    evaluation_config = candidate.get("evaluation_config")
    return not (
        isinstance(evaluation_config, dict)
        and isinstance(evaluation_config.get("evaluation_settings"), dict)
    )


@st.dialog("旧版で保存した候補の適合度")
def _legacy_evaluation_update_dialog(
    project_id: str,
    legacy_candidate_count: int,
) -> None:
    st.info(
        "旧版で保存された候補の適合度は、旧版当時の点数では"
        "ありません。現在画面で選択している評価条件を使って"
        "再計算した点数を表示します。"
    )
    st.write(f"対象となる保存候補: {legacy_candidate_count}件")
    st.caption(
        "旧候補を削除し、現在の評価条件で新しい候補を"
        "作成すると、"
        "この注意は表示されなくなります。"
    )
    if st.button(
        "閉じる",
        type="primary",
        key=f"close_legacy_evaluation_version_notice_{project_id}",
    ):
        st.rerun()


def render_legacy_candidate_evaluation_notice(
    project_id: str,
    candidates: list[dict[str, Any]],
) -> None:
    """Show once per browser session when legacy saved candidates are first read."""

    legacy_candidate_count = sum(
        _is_legacy_evaluation_candidate(candidate) for candidate in candidates
    )
    if legacy_candidate_count == 0:
        return
    shown_projects = _session_project_set(LEGACY_EVALUATION_NOTICE_SHOWN_KEY)
    if project_id in shown_projects or not _reserve_dialog():
        return
    shown_projects.add(project_id)
    _legacy_evaluation_update_dialog(project_id, legacy_candidate_count)
