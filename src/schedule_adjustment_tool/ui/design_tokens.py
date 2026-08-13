"""Semantic colors shared by the Python-rendered UI and calendar CSS."""

PRIMARY = "#175cd3"
DANGER = "#b42318"
UNIVERSITY_ROLE = PRIMARY
HIGH_SCHOOL_ROLE = "#222222"
SATURDAY_BACKGROUND = "rgba(82, 180, 255, 0.16)"
SATURDAY_FOREGROUND = PRIMARY
HOLIDAY_BACKGROUND = "rgba(255, 110, 110, 0.16)"
HOLIDAY_FOREGROUND = DANGER
# Pandas date-cell styling historically used a slightly stronger tint than
# the HTML calendar stylesheet. Keep that presentation while naming it.
CALENDAR_SATURDAY_BACKGROUND = "rgba(82, 180, 255, 0.18)"
CALENDAR_HOLIDAY_BACKGROUND = "rgba(255, 110, 110, 0.18)"
AVAILABILITY_HOLIDAY_BACKGROUND = "rgba(255, 99, 132, 0.14)"
ZOOM_BACKGROUND = "#dbeafe"
ZOOM_FOREGROUND = PRIMARY
ZOOM_BORDER = "#93c5fd"
TEXT = "#31333f"
DARK_TEXT = "#FAFAFA"
BORDER = "rgba(128, 128, 128, 0.28)"
CALENDAR_HEADER_BACKGROUND = "rgba(128, 128, 128, 0.1)"

# Descriptive aliases make the semantic role explicit at call sites that
# need a color rather than a background treatment.
PRIMARY_COLOR = PRIMARY
DANGER_COLOR = DANGER
UNIVERSITY_ROLE_COLOR = UNIVERSITY_ROLE
HIGH_SCHOOL_ROLE_COLOR = HIGH_SCHOOL_ROLE
SATURDAY = SATURDAY_BACKGROUND
HOLIDAY = HOLIDAY_BACKGROUND
ZOOM = ZOOM_BACKGROUND
