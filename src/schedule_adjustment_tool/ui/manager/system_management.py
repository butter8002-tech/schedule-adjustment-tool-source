"""System-administration navigation and compatibility exports.

Individual screens are kept in dedicated modules so a change to one operating
flow does not require reading unrelated account, participant, or recovery code.
The imports below preserve the established ``system_management`` public API for
the manager app and its tests during the transition.
"""

from __future__ import annotations

import streamlit as st

from schedule_adjustment_tool.ui.manager.system_accounts import (
    render_system_accounts_section,
)
from schedule_adjustment_tool.ui.manager.system_callbacks import (
    SystemManagementCallbacks,
    backup_restore_confirmation_dialog,
    common_participant_delete_confirmation_dialog,
    common_participant_table_updates,
    delete_project_confirmation_dialog,
    format_datetime_with_weekday,
    render_bulk_account_deletion,
    render_bulk_account_password_reset,
    render_individual_participant_account_generator,
    render_system_project_creator,
    reset_confirmation_dialog,
    system_management_callbacks,
)
from schedule_adjustment_tool.ui.manager.system_configuration import (
    render_system_maintenance_mode_section,
    render_system_settings_section,
)
from schedule_adjustment_tool.ui.manager.system_maintenance import (
    render_system_maintenance_section,
)
from schedule_adjustment_tool.ui.manager.system_participants import (
    render_system_participants_section,
)
from schedule_adjustment_tool.ui.manager.system_projects import (
    render_system_projects_section,
)
from schedule_adjustment_tool.ui.sidebar_navigation import (
    SidebarMenuItem,
    render_sidebar_menu,
)
from schedule_adjustment_tool.ui.application_metadata import APP_NAME


SYSTEM_MANAGEMENT_MENU_ITEMS = (
    SidebarMenuItem("全体設定", "全体設定", ":material/settings:", "期、データ保持、参加者向け説明を設定します。"),
    SidebarMenuItem("登録済み参加者", "登録済み参加者", ":material/groups:", "システムに登録されている参加者を管理します。"),
    SidebarMenuItem("アカウント・権限", "アカウント・権限", ":material/admin_panel_settings:", "ログイン用アカウントと権限を管理します。"),
    SidebarMenuItem("企画管理", "企画管理", ":material/event_note:", "企画の作成、複製、表示順を管理します。"),
    SidebarMenuItem("メンテナンス", "メンテナンス", ":material/construction:", "メンテナンス状態を切り替えます。"),
    SidebarMenuItem("保全・監査", "保全・監査", ":material/security:", "バックアップ、復元、監査ログを確認します。"),
)


def render_system_management() -> None:
    """Select and render exactly one system-administration operating flow."""

    with st.sidebar:
        st.title("システム管理")
        st.caption(APP_NAME)
        st.divider()
    section = render_sidebar_menu(
        SYSTEM_MANAGEMENT_MENU_ITEMS,
        state_key="system_management_section",
        default="全体設定",
        key_prefix="system_management",
        heading="管理メニュー",
    )
    st.caption(f"システム管理 ＞ {section}")
    if section == "登録済み参加者":
        render_system_participants_section()
    elif section == "アカウント・権限":
        render_system_accounts_section()
    elif section == "企画管理":
        render_system_projects_section()
    elif section == "メンテナンス":
        render_system_maintenance_mode_section()
    elif section == "保全・監査":
        render_system_maintenance_section()
    else:
        render_system_settings_section()
