"""Shared manager layout CSS kept outside the application composition root."""

from __future__ import annotations

import streamlit as st

from schedule_adjustment_tool.ui.styles import shared_page_styles


MAIN_PAGE_STYLES = shared_page_styles() + """
    <style>
    .st-key-manager_ui_workflow_footer button[kind="secondary"] {
        background: #e8eeff !important;
        border-color: #aebef2 !important;
        color: #2347ad !important;
    }
    .st-key-manager_ui_workflow_footer button[kind="secondary"]:hover {
        background: #dbe5ff !important;
        border-color: #8299e6 !important;
    }
    .st-key-manager_ui_workflow_footer button[kind="tertiary"] {
        background: transparent !important;
        border: 1px solid rgba(128, 128, 128, 0.45) !important;
        color: inherit !important;
    }
    .st-key-manager_ui_workflow_footer button[kind="tertiary"]:hover {
        background: rgba(128, 128, 128, 0.08) !important;
        border-color: rgba(128, 128, 128, 0.7) !important;
    }
    .st-key-manager_ui_workflow_footer button[kind="primary"] {
        background: #3157d5 !important;
        border-color: #2445b2 !important;
        color: #ffffff !important;
    }
    .st-key-manager_ui_workflow_footer button[kind="primary"]:hover {
        background: #2445b2 !important;
        border-color: #1b358b !important;
    }
    .priority-item {
        font-size: 1rem;
        font-weight: 650;
        line-height: 1.35;
        padding: 0.35rem 0.5rem;
        border-radius: 0.45rem;
        background: rgba(120, 120, 120, 0.08);
        margin-bottom: 0.25rem;
    }
    .section-heading {
        display: flex;
        align-items: center;
        gap: 0.35rem;
        margin: 0.2rem 0 0.55rem;
    }
    .section-heading h3, .section-heading h4, .section-heading h5,
    .section-heading h6 {
        margin: 0;
        padding: 0;
    }
    .evaluation-label {
        display: flex;
        align-items: center;
        gap: 0.32rem;
        min-height: 2.45rem;
        padding-top: 0.9rem;
        font-size: 0.94rem;
        font-weight: 650;
    }
    </style>
    """


def render_main_styles() -> None:
    """Re-inject layout CSS on every Streamlit rerun."""

    st.markdown(MAIN_PAGE_STYLES, unsafe_allow_html=True)
