"""Project ordering, archiving, and creation controls for administrators."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.storage import update_project_organization
from schedule_adjustment_tool.ui.manager.app_cache import (
    clear_audit_logs_cache,
    load_project_list_cached,
    status_message,
)
from schedule_adjustment_tool.ui.manager.project_cache import refresh_project_list_cache
from schedule_adjustment_tool.ui.manager.system_callbacks import (
    render_system_project_creator,
)


def render_system_projects_section() -> None:
    """Render project ordering and archive state before creation tools."""

    projects = load_project_list_cached()
    st.subheader("企画の表示・アーカイブ")
    st.caption("企画の表示順、アーカイブ、作成、複製を管理します。")
    organization_frame = pd.DataFrame(
        [
            {
                "ID": str(project["id"]),
                "企画名": str(project.get("title", "名称未設定")),
                "表示順": int(project.get("sort_order", index)),
                "アーカイブ": bool(project.get("archived", False)),
            }
            for index, project in enumerate(projects)
        ]
    )
    with st.form("project_organization_form"):
        edited_organization = st.data_editor(
            organization_frame,
            hide_index=True,
            width="stretch",
            disabled=["ID", "企画名"],
            column_config={
                "ID": st.column_config.TextColumn("ID", width="small"),
                "企画名": st.column_config.TextColumn("企画名"),
                "表示順": st.column_config.NumberColumn(
                    "表示順", min_value=0, step=1, required=True
                ),
                "アーカイブ": st.column_config.CheckboxColumn("アーカイブ"),
            },
            key="project_organization",
        )
        save_organization_clicked = st.form_submit_button(
            "企画の並び順・アーカイブを保存"
        )
    if save_organization_clicked:
        if bool(edited_organization["アーカイブ"].all()):
            st.warning("少なくとも1つの企画をアーカイブせずに残してください。")
        else:
            with status_message("企画の表示設定を保存しています..."):
                update_project_organization(
                    [
                        {
                            "id": str(row["ID"]),
                            "sort_order": int(row["表示順"]),
                            "archived": bool(row["アーカイブ"]),
                        }
                        for row in edited_organization.to_dict("records")
                    ]
                )
                refresh_project_list_cache()
                clear_audit_logs_cache()
            st.success("企画の表示設定を保存しました。")
            st.rerun()

    st.divider()
    st.subheader("企画の作成・複製")
    render_system_project_creator(projects, key_prefix="system")
