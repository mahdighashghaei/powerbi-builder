"""Calculation Group definitions for Power BI TMDL.

Each factory returns a group_def dict consumed by write_tmdl_calc_group:

    {
        "name": str,           # table name, e.g. "Time Intelligence"
        "precedence": int,     # optional; higher = evaluated first (default 10)
        "items": [
            {
                "name": str,               # item display name
                "expression": str,         # single-line DAX using SELECTEDMEASURE()
                "formatStringDefinition":  # optional DAX format override
                    str | None,
                            }
        ]
    }
"""
from __future__ import annotations


def time_intelligence(date_col: str = "'Date'[Date]",
                      precedence: int = 10) -> dict:
    """Standard time intelligence calculation group.

    Items: Current, YTD, QTD, MTD, PY, YoY Chg, YoY %, MAT (12M rolling).
    Requires a Date table with a Date column.
    """
    d = date_col
    return {
        "name": "Time Intelligence",
        "precedence": precedence,
        "items": [
            {
                "name": "Current",
                "expression": "SELECTEDMEASURE()",
                "ordinal": 0,
            },
            {
                "name": "YTD",
                "expression": f"CALCULATE(SELECTEDMEASURE(), DATESYTD({d}))",
                "ordinal": 1,
            },
            {
                "name": "QTD",
                "expression": f"CALCULATE(SELECTEDMEASURE(), DATESQTD({d}))",
                "ordinal": 2,
            },
            {
                "name": "MTD",
                "expression": f"CALCULATE(SELECTEDMEASURE(), DATESMTD({d}))",
                "ordinal": 3,
            },
            {
                "name": "PY",
                "expression": f"CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR({d}))",
                "ordinal": 4,
            },
            {
                "name": "YoY Chg",
                "expression": (
                    f"VAR _cur = SELECTEDMEASURE() "
                    f"VAR _py = CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR({d})) "
                    f"RETURN IF(NOT ISBLANK(_py), _cur - _py)"
                ),
                "ordinal": 5,
            },
            {
                "name": "YoY %",
                "expression": (
                    f"VAR _cur = SELECTEDMEASURE() "
                    f"VAR _py = CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR({d})) "
                    f"RETURN IF(NOT ISBLANK(_py), DIVIDE(_cur - _py, _py))"
                ),
                "formatStringDefinition": '"0.0%"',
                "ordinal": 6,
            },
            {
                "name": "MAT",
                "expression": (
                    f"CALCULATE(SELECTEDMEASURE(), "
                    f"DATESINPERIOD({d}, LASTDATE({d}), -12, MONTH))"
                ),
                "ordinal": 7,
            },
        ],
    }


def period_comparison(date_col: str = "'Date'[Date]",
                      precedence: int = 20) -> dict:
    """Period comparison group: Actual, Prior Period, Change, Change %.

    Useful for budget vs actuals or period-over-period comparisons.
    """
    d = date_col
    return {
        "name": "Period Comparison",
        "precedence": precedence,
        "items": [
            {
                "name": "Actual",
                "expression": "SELECTEDMEASURE()",
                "ordinal": 0,
            },
            {
                "name": "Prior Period",
                "expression": (
                    f"CALCULATE(SELECTEDMEASURE(), DATEADD({d}, -1, MONTH))"
                ),
                "ordinal": 1,
            },
            {
                "name": "Change",
                "expression": (
                    f"VAR _cur = SELECTEDMEASURE() "
                    f"VAR _pp = CALCULATE(SELECTEDMEASURE(), DATEADD({d}, -1, MONTH)) "
                    f"RETURN IF(NOT ISBLANK(_pp), _cur - _pp)"
                ),
                "ordinal": 2,
            },
            {
                "name": "Change %",
                "expression": (
                    f"VAR _cur = SELECTEDMEASURE() "
                    f"VAR _pp = CALCULATE(SELECTEDMEASURE(), DATEADD({d}, -1, MONTH)) "
                    f"RETURN IF(NOT ISBLANK(_pp), DIVIDE(_cur - _pp, _pp))"
                ),
                "formatStringDefinition": '"0.0%"',
                "ordinal": 3,
            },
        ],
    }


def currency_conversion(rate_measure: str = "[Exchange Rate]",
                        precedence: int = 30) -> dict:
    """Currency conversion group: Local, USD, EUR (extend as needed)."""
    return {
        "name": "Currency",
        "precedence": precedence,
        "items": [
            {
                "name": "Local",
                "expression": "SELECTEDMEASURE()",
                "ordinal": 0,
            },
            {
                "name": "USD",
                "expression": f"SELECTEDMEASURE() * {rate_measure}",
                "ordinal": 1,
            },
        ],
    }
