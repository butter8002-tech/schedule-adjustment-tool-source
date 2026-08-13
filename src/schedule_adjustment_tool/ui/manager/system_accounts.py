"""Account creation, authorization assignment, and account lifecycle controls."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from schedule_adjustment_tool.domain.app_config import load_app_settings
from schedule_adjustment_tool.domain.auth import (
    hash_password,
    invalidate_bootstrap_admin_cache,
)
from schedule_adjustment_tool.storage import (
    StorageError,
    assign_membership,
    create_user,
    delete_user,
    remove_membership,
    update_user,
)
from schedule_adjustment_tool.ui.manager.app_cache import (
    clear_audit_logs_cache,
    clear_pending_memberships,
    clear_pending_users,
    load_memberships_cached,
    load_project_list_cached,
    load_users_cached,
    pending_memberships,
    pending_users,
    refresh_memberships_cache,
    refresh_users_cache,
    status_message,
)
from schedule_adjustment_tool.ui.manager.system_callbacks import (
    render_bulk_account_deletion,
    render_bulk_account_password_reset,
    render_individual_participant_account_generator,
)


APP_SETTINGS = load_app_settings()


def render_system_accounts_section() -> None:
    """Render account and project-membership administration."""

    st.subheader("認証・権限")
    st.caption(
        "現在の認証状態: "
        + ("有効" if APP_SETTINGS.auth_required else "無効（ローカルデモ）")
    )
    st.info(
        "新規アカウントと権限割り当ては、まずこの画面の「保存待ち」へ追加します。"
        "内容を確認してから、保存してください。"
    )
    pending_columns = st.columns(2)
    pending_columns[0].metric("アカウント作成の保存待ち", f"{len(pending_users())}件")
    pending_columns[1].metric(
        "権限割り当ての保存待ち", f"{len(pending_memberships())}件"
    )
    projects = load_project_list_cached()
    with st.form("create_user_form", clear_on_submit=True):
        user_columns = st.columns([2, 2, 1, 1, 2, 2, 1])
        username = user_columns[0].text_input(
            "ユーザー名", max_chars=APP_SETTINGS.max_text_length
        )
        password = user_columns[1].text_input(
            "初期パスワード", type="password", max_chars=256
        )
        system_admin = user_columns[2].checkbox("管理者")
        participant_role = user_columns[3].checkbox("参加者（共通）")
        all_project_manager = user_columns[4].checkbox(
            "スケジュール担当共通アカウント（全企画）",
            help="ONにすると、企画ごとの権限割り当てなしで全企画を操作できます。",
        )
        manager_project_ids = user_columns[5].multiselect(
            "スケジュール担当企画",
            [str(project["id"]) for project in projects],
            format_func=lambda value: next(
                str(project["title"])
                for project in projects
                if str(project["id"]) == value
            ),
            disabled=all_project_manager,
        )
        create_user_clicked = user_columns[6].form_submit_button("保存待ちへ追加")
    if create_user_clicked:
        normalized_username = username.strip()
        existing_usernames = {
            str(user["username"]).casefold() for user in load_users_cached()
        }
        pending_usernames = {
            str(user["username"]).casefold() for user in pending_users()
        }
        if not normalized_username:
            st.warning("ユーザー名を入力してください。")
        elif normalized_username.casefold() in existing_usernames | pending_usernames:
            st.warning("同じユーザー名が既に存在するか、下書きにあります。")
        elif len(password) < 12:
            st.warning("パスワードは12文字以上にしてください。")
        else:
            pending_users().append(
                {
                    "username": normalized_username,
                    "password": password,
                    "is_system_admin": bool(system_admin),
                    "is_schedule_manager": bool(all_project_manager),
                    "is_participant": bool(participant_role),
                    "manager_project_ids": list(manager_project_ids),
                    "manager_project_titles": [
                        str(project["title"])
                        for project in projects
                        if str(project["id"]) in set(manager_project_ids)
                    ],
                }
            )
            st.success("アカウント作成を保存待ちへ追加しました。")
            st.rerun()

    staged_users = pending_users()
    if staged_users:
        st.markdown("**アカウント作成の保存待ち**")
        st.caption(
            "保存待ちの内容を上から順に保存します。"
            "途中でエラーが表示された場合は、保存済みの項目を確認してから"
            "残りをもう一度保存してください。"
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "ユーザー名": user["username"],
                        "管理者": bool(user["is_system_admin"]),
                        "スケジュール担当共通アカウント（全企画）": bool(
                            user.get("is_schedule_manager", False)
                        ),
                        "参加者（共通）": bool(user.get("is_participant", False)),
                        "スケジュール担当企画": "、".join(
                            user.get("manager_project_titles", [])
                        ),
                    }
                    for user in staged_users
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        draft_columns = st.columns(2)
        if draft_columns[0].button(
            f"保存待ち{len(staged_users)}件を保存", type="primary"
        ):
            try:
                with status_message("アカウント作成下書きを保存しています..."):
                    for user in staged_users:
                        user_id = create_user(
                            str(user["username"]),
                            hash_password(str(user["password"])),
                            is_system_admin=bool(user["is_system_admin"]),
                            is_schedule_manager=bool(
                                user.get("is_schedule_manager", False)
                            ),
                            is_participant=bool(user.get("is_participant", False)),
                            password_plain=str(user["password"]),
                            password_source="システム管理",
                        )
                        for project_id in user.get("manager_project_ids", []):
                            assign_membership(user_id, str(project_id), "manager", "")
                    clear_pending_users()
                    invalidate_bootstrap_admin_cache()
                    refresh_users_cache()
                    refresh_memberships_cache()
                    clear_audit_logs_cache()
                st.success("保存待ちのアカウントを保存しました。")
                st.rerun()
            except (ValueError, StorageError) as error:
                st.warning(str(error))
        if draft_columns[1].button("アカウント作成の保存待ちを破棄"):
            clear_pending_users()
            st.rerun()

    users = load_users_cached()
    if users:
        st.markdown("**アカウント編集**")
        edited_user_id = st.selectbox(
            "対象アカウント",
            [str(user["id"]) for user in users],
            format_func=lambda value: next(
                user["username"] for user in users if user["id"] == value
            ),
            key="edited_user_selector",
        )
        edited_user = next(user for user in users if user["id"] == edited_user_id)
        with st.form("edit_user_form"):
            account_columns = st.columns([1, 1, 1, 1, 2, 1])
            edited_active = account_columns[0].checkbox(
                "有効", value=bool(edited_user["active"]), key=f"user_active_{edited_user_id}"
            )
            edited_admin = account_columns[1].checkbox(
                "管理者",
                value=bool(edited_user["is_system_admin"]),
                key=f"user_admin_{edited_user_id}",
            )
            edited_participant = account_columns[2].checkbox(
                "参加者（共通）",
                value=bool(edited_user.get("is_participant", False)),
                key=f"user_participant_{edited_user_id}",
            )
            edited_schedule_manager = account_columns[3].checkbox(
                "スケジュール担当共通アカウント（全企画）",
                value=bool(edited_user.get("is_schedule_manager", False)),
                key=f"user_schedule_manager_{edited_user_id}",
            )
            new_password = account_columns[4].text_input(
                "新しいパスワード（変更時のみ）",
                type="password",
                max_chars=256,
                key=f"user_password_{edited_user_id}",
            )
            update_user_clicked = account_columns[5].form_submit_button("更新")
        if update_user_clicked:
            try:
                with status_message("アカウントを更新しています..."):
                    update_user(
                        edited_user_id,
                        is_system_admin=edited_admin,
                        is_schedule_manager=edited_schedule_manager,
                        is_participant=edited_participant,
                        active=edited_active,
                        password_hash=hash_password(new_password) if new_password else None,
                        password_plain=new_password if new_password else None,
                        password_source="システム管理" if new_password else "",
                    )
                    invalidate_bootstrap_admin_cache()
                    refresh_users_cache()
                    refresh_memberships_cache()
                    clear_audit_logs_cache()
                st.success("アカウントを更新しました。")
                st.rerun()
            except (ValueError, StorageError) as error:
                st.warning(str(error))
        if st.button("選択したアカウントを削除", key=f"delete_user_{edited_user_id}"):
            try:
                with status_message("アカウントを削除しています..."):
                    delete_user(edited_user_id)
                    invalidate_bootstrap_admin_cache()
                    refresh_users_cache()
                    refresh_memberships_cache()
                    clear_audit_logs_cache()
                st.success("アカウントを削除しました。")
                st.rerun()
            except StorageError as error:
                st.warning(str(error))

    if users and projects:
        membership_project_id = st.selectbox(
            "スケジュール担当者権限を割り当てる企画",
            [str(project["id"]) for project in projects],
            format_func=lambda value: next(
                project["title"] for project in projects if project["id"] == value
            ),
            key="membership_project_selector",
        )
        with st.form("membership_draft_form"):
            membership_columns = st.columns([2, 2, 1])
            membership_user_id = membership_columns[0].selectbox(
                "ユーザー",
                [str(user["id"]) for user in users],
                format_func=lambda value: next(
                    user["username"] for user in users if user["id"] == value
                ),
            )
            membership_columns[1].text_input(
                "権限", value="スケジュール担当者", disabled=True
            )
            membership_clicked = membership_columns[2].form_submit_button(
                "割り当てを保存待ちへ追加"
            )
        if membership_clicked:
            pending_memberships().append(
                {
                    "user_id": membership_user_id,
                    "username": next(
                        str(user["username"])
                        for user in users
                        if str(user["id"]) == membership_user_id
                    ),
                    "project_id": membership_project_id,
                    "project_title": next(
                        str(project["title"])
                        for project in projects
                        if str(project["id"]) == membership_project_id
                    ),
                    "role": "manager",
                    "participant_id": "",
                }
            )
            st.success("スケジュール担当者権限を保存待ちへ追加しました。")
            st.rerun()
    staged_memberships = pending_memberships()
    if staged_memberships:
        st.markdown("**企画内権限割り当ての保存待ち**")
        st.caption("保存待ちの権限割り当てを上から順に保存します。")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "ユーザー": item["username"],
                        "企画": item["project_title"],
                        "権限": "担当者" if item["role"] == "manager" else "参加者",
                    }
                    for item in staged_memberships
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        membership_draft_columns = st.columns(2)
        if membership_draft_columns[0].button(
            f"保存待ち{len(staged_memberships)}件を保存",
            type="primary",
            key="save_pending_memberships",
        ):
            try:
                with status_message("権限割り当て下書きを保存しています..."):
                    for item in staged_memberships:
                        assign_membership(
                            str(item["user_id"]),
                            str(item["project_id"]),
                            str(item["role"]),
                            str(item["participant_id"]),
                        )
                    clear_pending_memberships()
                    refresh_memberships_cache()
                    clear_audit_logs_cache()
                st.success("保存待ちの権限割り当てを保存しました。")
                st.rerun()
            except StorageError as error:
                st.warning(str(error))
        if membership_draft_columns[1].button("権限割り当ての保存待ちを破棄"):
            clear_pending_memberships()
            st.rerun()
    if users:
        with st.expander("アカウント一覧・一括削除"):
            st.dataframe(pd.DataFrame(users), hide_index=True, width="stretch")
            render_bulk_account_password_reset(users)
            st.divider()
            render_bulk_account_deletion(users)
    if users and projects and st.toggle(
        "参加者個別アカウントの一括発行を開く",
        value=False,
        key="show_individual_participant_account_generator",
        help="開くと、選択した企画の参加者一覧を表示します。",
    ):
        with st.container(border=True):
            render_individual_participant_account_generator(projects)
    memberships = load_memberships_cached()
    if memberships:
        with st.expander("権限一覧・権限解除"):
            st.dataframe(pd.DataFrame(memberships), hide_index=True, width="stretch")
            membership_labels = {
                (str(item["user_id"]), str(item["project_id"]), str(item["role"])): (
                    f"{item['username']} / {item['project_title']} / "
                    + (
                        "担当者"
                        if item["role"] == "manager"
                        else (
                            "参加者（全参加者）"
                            if not item.get("participant_id")
                            else "参加者"
                        )
                    )
                )
                for item in memberships
            }
            removal_key = st.selectbox(
                "解除する企画内権限",
                list(membership_labels),
                format_func=lambda value: membership_labels[value],
            )
            if st.button("選択した企画内権限を解除"):
                with status_message("企画内権限を解除しています..."):
                    remove_membership(*removal_key)
                    refresh_memberships_cache()
                    clear_audit_logs_cache()
                st.success("企画内権限を解除しました。")
                st.rerun()
