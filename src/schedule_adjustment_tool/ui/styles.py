"""Shared base layout and schedule-calendar styles for Streamlit screens."""

from schedule_adjustment_tool.ui.design_tokens import (
    BORDER,
    CALENDAR_HEADER_BACKGROUND,
    DANGER,
    HIGH_SCHOOL_ROLE,
    HOLIDAY_BACKGROUND,
    SATURDAY_BACKGROUND,
    TEXT,
    UNIVERSITY_ROLE,
    ZOOM_BACKGROUND,
    ZOOM_BORDER,
    ZOOM_FOREGROUND,
)


def shared_page_styles() -> str:
    """Return the common page layout and schedule-calendar CSS."""

    return f"""
    <style>
    .stMainBlockContainer,
    .block-container,
    [data-testid="stMainBlockContainer"],
    [data-testid="block-container"],
    [data-testid="stAppViewContainer"] > .main .block-container,
    [data-testid="stAppViewContainer"] .block-container {{
        max-width: 1480px !important;
        width: calc(100% - 2rem) !important;
        margin-left: auto !important;
        margin-right: auto !important;
        padding-left: 1rem !important;
        padding-right: 1rem !important;
    }}
    .stMainBlockContainer [data-testid="stVerticalBlock"],
    .block-container [data-testid="stVerticalBlock"] {{
        width: 100%;
    }}
    [data-testid="stElementContainer"]:has(iframe[title*="availability_grid"]),
    [data-testid="element-container"]:has(iframe[title*="availability_grid"]),
    div:has(> iframe[title*="availability_grid"]) {{
        max-width: none !important;
        width: 100% !important;
    }}
    iframe[title*="availability_grid"] {{
        max-width: none !important;
        width: 100% !important;
        min-width: 0 !important;
    }}
    [data-testid="stElementContainer"]:has(iframe[title*="schedule_calendar_editor"]),
    [data-testid="element-container"]:has(iframe[title*="schedule_calendar_editor"]),
    div:has(> iframe[title*="schedule_calendar_editor"]),
    iframe[title*="schedule_calendar_editor"] {{
        max-width: none !important;
        width: 100% !important;
        min-width: 0 !important;
    }}
    [data-testid="stSidebar"] {{
        color: {TEXT} !important;
    }}
    [data-testid="stSidebar"] [data-baseweb="select"],
    [data-testid="stSidebar"] [data-baseweb="select"] * {{
        color: {TEXT} !important;
        -webkit-text-fill-color: currentColor !important;
    }}
    .schedule-calendar {{
        max-width: none;
        border-collapse: collapse;
        table-layout: fixed;
        font-size: 0.9rem;
    }}
    .schedule-calendar-wrapper {{
        width: 100%;
        max-width: 100%;
        overflow-x: auto;
    }}
    .schedule-calendar th, .schedule-calendar td {{
        border: 1px solid {BORDER};
        padding: 0.55rem;
        vertical-align: top;
        white-space: pre-line;
        overflow-wrap: anywhere;
    }}
    .schedule-calendar th {{ background: {CALENDAR_HEADER_BACKGROUND}; }}
    .schedule-calendar .saturday {{
        background: {SATURDAY_BACKGROUND};
        color: {UNIVERSITY_ROLE};
    }}
    .schedule-calendar .holiday {{
        background: {HOLIDAY_BACKGROUND};
        color: {DANGER};
    }}
    .schedule-calendar .university-role {{
        color: {UNIVERSITY_ROLE};
    }}
    .schedule-calendar .high-school-role {{
        color: {HIGH_SCHOOL_ROLE};
    }}
    .schedule-calendar .meeting-chip {{
        display: inline-block;
        padding: 0.08rem 0.42rem;
        border-radius: 999px;
        background: {ZOOM_BACKGROUND};
        color: {ZOOM_FOREGROUND};
        border: 1px solid {ZOOM_BORDER};
        font-size: 0.78rem;
        font-weight: 700;
        line-height: 1.35;
        margin-bottom: 0.18rem;
    }}
    </style>
    """
