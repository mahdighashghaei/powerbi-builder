"""Date table TMDL generator.

Generates a complete Date dimension table TMDL file with:
- Full calendar columns (Year, Quarter, Month, Week, Day)
- Fiscal year support (optional)
- TMDL-valid syntax with proper lineageTags and summarizeBy

The Date table M expression uses CALENDAR() to auto-expand from
the minimum to maximum date in any fact table date column.
This avoids hard-coded date ranges.
"""

from __future__ import annotations

import uuid
from typing import Any


def _tag() -> str:
    return str(uuid.uuid4())


def build_date_table_tmdl(
    fact_table: str,
    date_column: str,
    fiscal_year_start_month: int = 1,
) -> str:
    """Return the full TMDL text for a Date dimension table.

    Args:
        fact_table:   Name of the fact table containing the date column.
        date_column:  Name of the date column in the fact table.
        fiscal_year_start_month: Month number (1-12) when fiscal year starts.
    """
    fiscal = fiscal_year_start_month != 1
    fy_offset = fiscal_year_start_month - 1

    tbl_tag = _tag()
    col_tags = {name: _tag() for name in [
        "Date", "Year", "YearMonthNum", "YearMonth",
        "Quarter", "QuarterNum", "Month", "MonthNum", "MonthShort",
        "Week", "WeekNum", "Day", "DayOfWeek", "DayOfWeekShort",
        "IsWeekend", "IsCurrentYear", "IsCurrentMonth",
    ]}
    if fiscal:
        col_tags.update({n: _tag() for n in [
            "FiscalYear", "FiscalQuarter", "FiscalMonth"
        ]})

    lines: list[str] = []

    lines.append("table Date")
    lines.append(f"\tlineageTag: {tbl_tag}")
    lines.append("")

    def col(name: str, dtype: str, summarize: str = "none",
            fmt: str | None = None, hidden: bool = False) -> None:
        lines.append(f"\tcolumn {name}")
        lines.append(f"\t\tdataType: {dtype}")
        lines.append(f"\t\tlineageTag: {col_tags[name]}")
        lines.append(f"\t\tsummarizeBy: {summarize}")
        lines.append(f"\t\tsourceColumn: {name}")
        if fmt:
            lines.append(f'\t\tformatString: {fmt}')
        if hidden:
            lines.append("\t\tisHidden")
        lines.append("")

    col("Date", "dateTime", fmt="Short Date")
    col("Year", "int64", "none")
    col("YearMonthNum", "int64", "none", hidden=True)
    col("YearMonth", "string", "none")
    col("Quarter", "string", "none")
    col("QuarterNum", "int64", "none", hidden=True)
    col("Month", "string", "none")
    col("MonthNum", "int64", "none", hidden=True)
    col("MonthShort", "string", "none")
    col("Week", "string", "none")
    col("WeekNum", "int64", "none", hidden=True)
    col("Day", "int64", "none")
    col("DayOfWeek", "string", "none")
    col("DayOfWeekShort", "string", "none")
    col("IsWeekend", "boolean", "none")
    col("IsCurrentYear", "boolean", "none")
    col("IsCurrentMonth", "boolean", "none")

    if fiscal:
        col("FiscalYear", "string", "none")
        col("FiscalQuarter", "string", "none")
        col("FiscalMonth", "int64", "none")

    # M partition using CALENDAR over fact table date range
    lines.append(f"\tpartition Date = m")
    lines.append(f"\t\tmode: import")
    lines.append(f"\t\tqueryGroup: Tables")
    lines.append(f"\t\tsource =")
    lines.append(f"\t\t\tlet")
    lines.append(f'\t\t\t\tMinDate = List.Min({fact_table}[{date_column}]),')
    lines.append(f'\t\t\t\tMaxDate = List.Max({fact_table}[{date_column}]),')
    lines.append(f'\t\t\t\tDateList = List.Dates(Date.From(MinDate), Duration.Days(Date.From(MaxDate) - Date.From(MinDate)) + 1, #duration(1,0,0,0)),')
    lines.append(f'\t\t\t\tDateTable = Table.FromList(DateList, Splitter.SplitByNothing(), {{"Date"}}, null, ExtraValues.Error),')
    lines.append(f'\t\t\t\tTyped = Table.TransformColumnTypes(DateTable, {{"Date", type date}}),')
    lines.append(f'\t\t\t\tWithYear = Table.AddColumn(Typed, "Year", each Date.Year([Date]), Int64.Type),')
    lines.append(f'\t\t\t\tWithYMN = Table.AddColumn(WithYear, "YearMonthNum", each Date.Year([Date]) * 100 + Date.Month([Date]), Int64.Type),')
    lines.append(f'\t\t\t\tWithYM = Table.AddColumn(WithYMN, "YearMonth", each Text.From(Date.Year([Date])) & "-" & Text.PadStart(Text.From(Date.Month([Date])), 2, "0"), type text),')
    lines.append(f'\t\t\t\tWithQ = Table.AddColumn(WithYM, "Quarter", each "Q" & Text.From(Date.QuarterOfYear([Date])), type text),')
    lines.append(f'\t\t\t\tWithQN = Table.AddColumn(WithQ, "QuarterNum", each Date.QuarterOfYear([Date]), Int64.Type),')
    lines.append(f'\t\t\t\tWithM = Table.AddColumn(WithQN, "Month", each Date.MonthName([Date]), type text),')
    lines.append(f'\t\t\t\tWithMN = Table.AddColumn(WithM, "MonthNum", each Date.Month([Date]), Int64.Type),')
    lines.append(f'\t\t\t\tWithMS = Table.AddColumn(WithMN, "MonthShort", each Text.Start(Date.MonthName([Date]), 3), type text),')
    lines.append(f'\t\t\t\tWithW = Table.AddColumn(WithMS, "Week", each "W" & Text.PadStart(Text.From(Date.WeekOfYear([Date])), 2, "0"), type text),')
    lines.append(f'\t\t\t\tWithWN = Table.AddColumn(WithW, "WeekNum", each Date.WeekOfYear([Date]), Int64.Type),')
    lines.append(f'\t\t\t\tWithD = Table.AddColumn(WithWN, "Day", each Date.Day([Date]), Int64.Type),')
    lines.append(f'\t\t\t\tWithDW = Table.AddColumn(WithD, "DayOfWeek", each Date.DayOfWeekName([Date]), type text),')
    lines.append(f'\t\t\t\tWithDWS = Table.AddColumn(WithDW, "DayOfWeekShort", each Text.Start(Date.DayOfWeekName([Date]), 3), type text),')
    lines.append(f'\t\t\t\tWithWE = Table.AddColumn(WithDWS, "IsWeekend", each Date.DayOfWeek([Date]) >= 5, type logical),')
    lines.append(f'\t\t\t\tWithCY = Table.AddColumn(WithWE, "IsCurrentYear", each Date.Year([Date]) = Date.Year(DateTime.LocalNow()), type logical),')
    lines.append(f'\t\t\t\tResult = Table.AddColumn(WithCY, "IsCurrentMonth", each Date.Year([Date]) = Date.Year(DateTime.LocalNow()) and Date.Month([Date]) = Date.Month(DateTime.LocalNow()), type logical)')
    lines.append(f"\t\t\tin")
    lines.append(f"\t\t\t\tResult")
    lines.append("")
    lines.append("\tannotation PBI_ResultType = Table")
    lines.append("\tannotation PBI_NavigationStepName = Navigation")
    lines.append("")
    lines.append(f"\tannotation $name = Date")
    lines.append(f"\tannotation $is_date_table = 1")
    lines.append("")

    return "\n".join(lines)


def needs_date_table(schema: dict) -> tuple[bool, str, str]:
    """Check if the schema has a datetime column suitable for a Date table.

    Returns (should_create, fact_table_name, date_column_name).
    """
    table_name = schema.get("table_name", "")
    for col in schema.get("columns", []):
        if col.get("dataType") in {"dateTime", "date"}:
            return True, table_name, col["name"]
    return False, "", ""
