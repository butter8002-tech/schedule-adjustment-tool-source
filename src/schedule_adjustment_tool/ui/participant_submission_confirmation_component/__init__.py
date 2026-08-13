from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit.components.v1 as components


_COMPONENT_PATH = Path(__file__).parent / "frontend"
_participant_submission_confirmation = components.declare_component(
    "participant_submission_confirmation_v1",
    path=str(_COMPONENT_PATH),
)


def participant_submission_confirmation(
    participant_name: str,
    *,
    initial_value: str,
    has_updates: bool,
    max_chars: int,
    state_id: str,
    key: str,
) -> dict[str, Any] | None:
    """Render the local name check and return only explicit button actions.

    The component compares the confirmation text in the browser.  Typing does
    not send a value to Streamlit or the database; a value is sent only after
    the user clicks one of the two buttons.
    """

    value = _participant_submission_confirmation(
        participant_name=participant_name,
        initial_value=initial_value,
        has_updates=bool(has_updates),
        max_chars=max(1, int(max_chars)),
        state_id=state_id,
        key=key,
        default=None,
    )
    return value if isinstance(value, dict) else None
