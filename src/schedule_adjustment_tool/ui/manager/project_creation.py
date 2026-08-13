"""Project creation and copy controls shared by manager and system screens."""

from __future__ import annotations

from schedule_adjustment_tool.domain.app_config import load_app_settings
from schedule_adjustment_tool.domain.auth import (
    Principal,
    hash_project_access_password,
)
from schedule_adjustment_tool.domain.models import Config
from schedule_adjustment_tool.storage import (
    StorageError,
    assign_membership,
    create_project,
    load_config,
)
from schedule_adjustment_tool.ui.manager.app_cache import (
    clear_audit_logs_cache,
    refresh_memberships_cache,
    status_message,
)
from schedule_adjustment_tool.ui.manager.project_cache import (
    load_project_data_cached,
    refresh_project_data_cache,
    refresh_project_list_cache,
)
from schedule_adjustment_tool.ui.manager.session_state import (
    manager_completed_steps,
    manager_dirty_steps,
    manager_review_steps,
    manager_started_steps,
    manager_status_overrides,
    set_manager_status_overrides,
)
from schedule_adjustment_tool.ui.manager.view_models import (
    build_manager_screen_context,
)
from schedule_adjustment_tool.ui.manager.workflow_state import (
    derive_workflow_states,
)

import streamlit as st


APP_SETTINGS = load_app_settings()


def project_config_for_creation(
    project_password: str,
    *,
    copy_from_project_id: str | None = None,
) -> Config:
    """Build settings for a copied project without inheriting credentials."""

    config = (
        Config.from_dict(load_config(copy_from_project_id).to_dict())
        if copy_from_project_id
        else Config()
    )
    config.status = "draft"
    # Access credentials must never be silently shared with a copied project.
    config.project_access_password_hash = ""
    if project_password:
        config.project_access_password_hash = hash_project_access_password(
            project_password
        )
    return config


def copied_project_workflow_statuses(project_id: str) -> dict[str, str]:
    """Carry over active workflow shape but require review in the new project."""

    data = load_project_data_cached(project_id)
    context = build_manager_screen_context(
        project_id,
        data["config"],
        data["participants"],
        data["candidates"],
        data["confirmed"],
    )
    states = derive_workflow_states(
        context.summary,
        dirty_steps=manager_dirty_steps(project_id),
        review_steps=manager_review_steps(project_id),
        started_steps=manager_started_steps(project_id),
        completed_steps=manager_completed_steps(project_id),
        status_overrides=manager_status_overrides(project_id),
    )
    statuses = {
        state.step_id: (
            "needs_review"
            if state.status.value == "complete"
            else state.status.value
        )
        for state in states
    }
    statuses["publish"] = "not_started"
    return statuses


def _render_new_project_form(
    projects: list[dict],
    *,
    key_prefix: str,
    principal: Principal | None = None,
    compact: bool = False,
) -> None:
    """Render and handle the shared project creation form."""

    st.caption(
        "選択中の企画パスワードは不要です。"
        "新しい企画の操作パスワードは任意で設定できます。"
    )
    project_ids = [str(project["id"]) for project in projects]
    copy_options = project_ids or [""]

    def project_title(project_id: str) -> str:
        if not project_id:
            return "（複製元なし）"
        return next(
            (
                str(project.get("title", "名称未設定"))
                for project in projects
                if str(project.get("id")) == project_id
            ),
            "名称未設定",
        )

    if compact:
        creation_mode = st.selectbox(
            "作成方法",
            ["空の企画", "設定を複製", "設定と名簿を複製"],
            key=f"{key_prefix}_creation_mode",
        )
        copy_source = st.selectbox(
            "複製元",
            copy_options,
            format_func=project_title,
            disabled=creation_mode == "空の企画" or not project_ids,
            key=f"{key_prefix}_copy_source",
        )
        with st.form(f"{key_prefix}_new_project_form", clear_on_submit=True):
            new_title = st.text_input(
                "新しい企画名",
                max_chars=APP_SETTINGS.max_text_length,
            )
            project_password = st.text_input(
                "企画操作パスワード（任意）",
                type="password",
            )
            create_clicked = st.form_submit_button(
                "企画を作成",
                type="primary",
                width="stretch",
            )
    else:
        # Forms do not rerun when a contained selectbox changes.  Keep the
        # mode selector outside so choosing a copy mode immediately enables
        # the source selector rendered in the form below.
        creation_mode = st.selectbox(
            "作成方法",
            ["空の企画", "設定を複製", "設定と名簿を複製"],
            key=f"{key_prefix}_creation_mode",
        )
        with st.form(f"{key_prefix}_new_project_form", clear_on_submit=True):
            columns = st.columns([3, 2, 2, 2, 1])
            new_title = columns[0].text_input(
                "新しい企画名",
                max_chars=APP_SETTINGS.max_text_length,
            )
            copy_source = columns[2].selectbox(
                "複製元",
                copy_options,
                format_func=project_title,
                disabled=creation_mode == "空の企画" or not project_ids,
                key=f"{key_prefix}_copy_source",
            )
            project_password = columns[3].text_input(
                "企画操作パスワード（任意）",
                type="password",
            )
            create_clicked = columns[4].form_submit_button(
                "作成",
                type="primary",
            )

    if not create_clicked:
        return
    if not new_title.strip():
        st.warning("企画名を入力してください。")
        return
    copy_source_id = copy_source if creation_mode != "空の企画" else None
    if creation_mode != "空の企画" and not copy_source_id:
        st.warning("複製元を選択してください。")
        return

    try:
        with status_message("企画を作成しています..."):
            new_project_id = create_project(
                new_title,
                config=project_config_for_creation(
                    project_password,
                    copy_from_project_id=copy_source_id,
                ),
                copy_from_project_id=copy_source_id,
                copy_participants=creation_mode == "設定と名簿を複製",
            )
            if copy_source_id:
                set_manager_status_overrides(
                    new_project_id,
                    copied_project_workflow_statuses(copy_source_id),
                )
            if (
                principal is not None
                and not principal.is_system_admin
                and not principal.is_schedule_manager
            ):
                assign_membership(
                    principal.user_id,
                    new_project_id,
                    "manager",
                    "",
                )
                principal.memberships.append(
                    {
                        "project_id": new_project_id,
                        "role": "manager",
                        "participant_id": "",
                    }
                )
            refresh_project_list_cache()
            refresh_project_data_cache(new_project_id)
            refresh_memberships_cache()
            clear_audit_logs_cache()
        st.session_state["active_project_id"] = new_project_id
        st.session_state["project_creation_notice"] = "企画を作成しました。"
        st.rerun()
    except (ValueError, StorageError) as error:
        st.warning(str(error))


def render_system_project_creator(
    projects: list[dict],
    *,
    key_prefix: str,
    principal: Principal | None = None,
) -> None:
    """Render project creation from the system-administration screen."""

    with st.expander("新しい企画を作成"):
        _render_new_project_form(
            projects,
            key_prefix=key_prefix,
            principal=principal,
        )


@st.dialog("新しい企画を作成", width="large")
def manager_project_creation_dialog(
    projects: list[dict],
    principal: Principal,
) -> None:
    _render_new_project_form(
        projects,
        key_prefix="manager_dialog",
        principal=principal,
        compact=True,
    )
