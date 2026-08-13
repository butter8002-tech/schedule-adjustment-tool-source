"""On-demand spreadsheet exports for the manager UI.

The workbook layouts remain in :mod:`schedule_adjustment_tool.exports`; this
module owns only the manager-facing preparation and download controls.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import streamlit as st

from schedule_adjustment_tool.domain.evaluation_config import EVALUATION_SCORE_VERSION
from schedule_adjustment_tool.domain.models import Config, Participant
from schedule_adjustment_tool.exports.spreadsheet_exports import (
    candidates_workbook,
    confirmed_schedule_workbook,
    input_status_workbook,
)
from schedule_adjustment_tool.storage import (
    StorageError,
    load_candidates_with_version,
    load_config,
    load_confirmed_candidate,
    load_participants,
)


@dataclass(frozen=True)
class ExportScreenServices:
    """Entry-point cache and audit coordination for deferred exports."""

    prepared_exports: Callable[[], dict[str, dict[str, Any]]]
    prepared_export_cache_limit: int
    run_with_status: Callable[..., Any]
    record_audit_event: Callable[..., None]
    scheduling_target_participants: Callable[
        [list[Participant]], list[Participant]
    ]


def project_input_status_export_bytes(
    project_id: str,
    *,
    services: ExportScreenServices,
) -> bytes:
    config = load_config(project_id)
    participants = services.scheduling_target_participants(
        load_participants(project_id)
    )
    return input_status_workbook(config, participants)


def project_candidates_export_bytes(project_id: str) -> bytes:
    config = load_config(project_id)
    candidates, _version = load_candidates_with_version(project_id)
    if not candidates:
        raise ValueError("出力できる保存候補がありません。")
    participants = load_participants(project_id)
    from schedule_adjustment_tool.domain.scheduler import (
        candidate_has_evaluation_config,
    )

    if any(
        candidate.get("metrics", {}).get("evaluation_score_version")
        != EVALUATION_SCORE_VERSION
        or not candidate_has_evaluation_config(candidate)
        for candidate in candidates
    ):
        from schedule_adjustment_tool.domain.scheduler import (
            candidate_sort_key,
            refresh_candidate_evaluation,
        )

        candidates = [
            refresh_candidate_evaluation(candidate, config, participants)
            for candidate in candidates
        ]
        candidates.sort(key=candidate_sort_key)
    return candidates_workbook(config, candidates, participants)


def project_confirmed_export_bytes(project_id: str) -> bytes:
    config = load_config(project_id)
    confirmed = load_confirmed_candidate(project_id)
    if confirmed is None:
        raise ValueError("出力できる確定日程がありません。")
    participants = load_participants(project_id)
    return confirmed_schedule_workbook(config, confirmed, participants)


def render_prepared_download(
    container: Any,
    *,
    project_id: str,
    kind: str,
    cache_token: str,
    prepare_label: str,
    download_label: str,
    status_label: str,
    build: Callable[..., bytes],
    build_args: tuple = (),
    file_name: str,
    audit_action: str,
    services: ExportScreenServices,
) -> None:
    """Build an expensive export only after the user explicitly requests it."""

    cache_key = f"{project_id}:{kind}:{cache_token}"
    cache = services.prepared_exports()
    if cache_key not in cache:
        container.caption(
            "必要な時だけ準備します。準備後にダウンロードボタンが表示されます。"
        )
    if cache_key not in cache and container.button(
        prepare_label,
        key=f"prepare_{kind}_{project_id}_{cache_token}",
    ):
        cache[cache_key] = {
            "project_id": project_id,
            "data": services.run_with_status(status_label, build, *build_args),
        }
        while len(cache) > services.prepared_export_cache_limit:
            cache.pop(next(iter(cache)))
    prepared = cache.get(cache_key)
    if prepared:
        container.success("準備できました。下のボタンからダウンロードできます。")
        container.download_button(
            download_label,
            prepared["data"],
            file_name=file_name,
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            key=f"download_{kind}_{project_id}_{cache_token}",
            on_click=services.record_audit_event,
            args=(audit_action,),
            kwargs={"project_id": project_id},
        )


def render_on_demand_project_export(
    *,
    project_id: str,
    kind: str,
    title: str,
    description: str,
    prepare_label: str,
    download_label: str,
    status_label: str,
    file_name: str,
    audit_action: str,
    build: Callable[[str], bytes],
    services: ExportScreenServices,
) -> None:
    cache_key = f"{project_id}:{kind}:on_demand"
    cache = services.prepared_exports()
    prepared = cache.get(cache_key)
    with st.container(border=True):
        st.markdown(f"**{title}**")
        st.caption(description)
        button_label = prepare_label if prepared is None else "最新データで再準備"
        if st.button(button_label, key=f"prepare_{kind}_{project_id}"):
            try:
                data = services.run_with_status(status_label, build, project_id)
            except (StorageError, ValueError) as error:
                st.warning(str(error))
            else:
                cache[cache_key] = {"project_id": project_id, "data": data}
                while len(cache) > services.prepared_export_cache_limit:
                    cache.pop(next(iter(cache)))
                prepared = cache[cache_key]
        if prepared is None:
            st.caption("準備ボタンを押すまで、出力用データは読み込みません。")
            return
        st.success("準備できました。")
        st.download_button(
            download_label,
            prepared["data"],
            file_name=file_name,
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            key=f"download_{kind}_{project_id}",
            on_click=services.record_audit_event,
            args=(audit_action,),
            kwargs={"project_id": project_id},
        )


def render_project_participant_exports(
    project_id: str,
    config: Config,
    participants: list[Participant],
    *,
    services: ExportScreenServices,
) -> None:
    """Render independent, on-demand exports for one project."""

    del participants  # Export data is read only after a prepare action.
    st.caption(
        "必要な出力だけを選んで準備します。"
        "画面を開いただけでは出力用データを読み込みません。"
    )
    export_definitions = (
        {
            "kind": "input_status_data",
            "title": "入力状況の出力",
            "description": "提出状況、回答内容、期間全体カレンダーを出力します。",
            "prepare_label": "入力状況を準備",
            "download_label": "入力状況をExcel出力",
            "status_label": "入力状況を読み込んでいます...",
            "file_name": f"{config.title}_入力状況.xlsx",
            "audit_action": "input_status_workbook.exported",
            "build": lambda current_project_id: project_input_status_export_bytes(
                current_project_id,
                services=services,
            ),
        },
        {
            "kind": "candidate_data",
            "title": "日程候補の出力",
            "description": "候補サマリー、個人別サマリー、一覧、期間全体カレンダーを出力します。",
            "prepare_label": "日程候補を準備",
            "download_label": "日程候補をExcel出力",
            "status_label": "保存候補を読み込んでいます...",
            "file_name": f"{config.title}_日程候補.xlsx",
            "audit_action": "candidates_workbook.exported",
            "build": project_candidates_export_bytes,
        },
        {
            "kind": "confirmed_schedule_data",
            "title": "確定日程の出力",
            "description": "現在公開中の確定日程を出力します。",
            "prepare_label": "確定日程を準備",
            "download_label": "確定日程をExcel出力",
            "status_label": "確定日程を読み込んでいます...",
            "file_name": f"{config.title}_確定日程.xlsx",
            "audit_action": "confirmed_schedule_workbook.exported",
            "build": project_confirmed_export_bytes,
        },
    )
    for definition in export_definitions:
        render_on_demand_project_export(
            project_id=project_id,
            services=services,
            **definition,
        )
