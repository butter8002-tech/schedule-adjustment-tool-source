from __future__ import annotations

from dataclasses import dataclass

import streamlit as st


@dataclass(frozen=True)
class SidebarMenuItem:
    id: str
    label: str
    icon: str
    help: str = ""


def _select_sidebar_menu_item(state_key: str, item_id: str) -> None:
    st.session_state[state_key] = item_id


def render_sidebar_menu(
    items: tuple[SidebarMenuItem, ...],
    *,
    state_key: str,
    default: str,
    key_prefix: str,
    heading: str = "",
) -> str:
    item_ids = {item.id for item in items}
    current = str(st.session_state.get(state_key, default))
    if current not in item_ids:
        current = default
    st.session_state[state_key] = current

    with st.sidebar:
        if heading:
            st.subheader(heading)
        for item in items:
            st.button(
                item.label,
                key=f"{key_prefix}_{item.id}",
                type="primary" if item.id == current else "tertiary",
                icon=item.icon,
                help=item.help or None,
                width="stretch",
                on_click=_select_sidebar_menu_item,
                args=(state_key, item.id),
            )
    return current
