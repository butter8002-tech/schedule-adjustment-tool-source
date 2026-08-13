"""Small, one-pass CSV helpers for user-controlled account exports."""

from __future__ import annotations

import csv
from io import StringIO
from typing import Any

import pandas as pd


def neutralize_csv_cell(value: Any) -> Any:
    """Prefix Excel formula-like text without changing the source dataframe."""

    if value is None or (
        pd.api.types.is_scalar(value) and bool(pd.isna(value))
    ):
        return ""
    if not isinstance(value, str):
        return value
    leading_text = value.lstrip()
    if value.startswith(("\t", "\r", "\n")) or (
        leading_text and leading_text[0] in "=+-@"
    ):
        return "'" + value
    return value


def dataframe_csv_bytes(frame: pd.DataFrame) -> bytes:
    """Serialize a dataframe once with formula-like text neutralized."""

    output = StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(frame.columns)
    for row in frame.itertuples(index=False, name=None):
        writer.writerow(neutralize_csv_cell(value) for value in row)
    return ("\ufeff" + output.getvalue()).encode("utf-8")
