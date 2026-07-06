"""PBIR generator -- builds Power BI Enhanced Report (PBIR) JSON payloads.

All payloads here are derived from real Power BI Desktop output (the
``data-goblin/power-bi-agentic-development`` reference samples) so they match
the schemas Power BI Desktop validates against. The PBIR format is *fragile*:
a single unknown property or wrong $schema URL will cause Desktop to refuse the
file, so this module is intentionally literal.

It also supports Deneb (Vega-Lite) custom visuals via :func:`build_deneb_visual`
— Deneb appears in PBIR as a visualType ``deneb7E15AEF80B9E4D4F8E12924291ECE89A``
whose ``objects.vega[0].properties.jsonSpec`` carries the Vega-Lite spec as a
JSON-encoded string literal.

Layout produced (matching the reference):

    <Name>.Report/
        .platform                                  # item type + logicalId
        definition.pbir                            # report entry (at ROOT)
        definition/
            version.json                           # PBIR format version (2.0.0)
            report.json                            # report root (report/3.0.0)
            pages/
                pages.json                         # pageOrder + activePageName
                <pageId>/
                    page.json                      # page (page/2.0.0)
                    visuals/
                        <visualId>/
                            visual.json            # visual (visualContainer/2.4.0)

The functions are pure (return dicts) so they can be unit-tested without disk
I/O; the ReportAgent + MCP tool are responsible for writing them.
"""

from __future__ import annotations

from typing import Any

from utils import stable_uuid

# ---------------------------------------------------------------------------
# Schemas (exact URLs from the reference samples -- do NOT change these)
# ---------------------------------------------------------------------------

_SCHEMA_PLATFORM = (
    "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
    "platformProperties/2.0.0/schema.json"
)
_SCHEMA_DEFINITION_PBIR = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definitionProperties/2.0.0/schema.json"
)
_SCHEMA_DEFINITION_PBISM = (
    "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/"
    "definitionProperties/1.0.0/schema.json"
)
_SCHEMA_VERSION = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
    "versionMetadata/1.0.0/schema.json"
)
_SCHEMA_PAGES = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
    "pagesMetadata/1.0.0/schema.json"
)
_SCHEMA_REPORT = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
    "report/3.0.0/schema.json"
)
_SCHEMA_PAGE = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
    "page/2.0.0/schema.json"
)
_SCHEMA_VISUAL = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
    "visualContainer/2.7.0/schema.json"
)


# ---------------------------------------------------------------------------
# Item / entry files
# ---------------------------------------------------------------------------


def platform_properties(item_type: str, display_name: str,
                        logical_id: str | None = None) -> dict[str, Any]:
    """``.platform`` file for an item (Report or SemanticModel).

    NOTE: the current PBIP layout uses ``.platform`` (a hidden file) at the
    root of each item folder -- NOT ``item.config.json``. Both carry the
    logicalId, but Desktop reads ``.platform``.
    """
    return {
        "$schema": _SCHEMA_PLATFORM,
        "metadata": {
            "type": item_type,          # "Report" | "SemanticModel"
            "displayName": display_name,
        },
        "config": {
            "version": "2.0",
            "logicalId": logical_id or stable_uuid(),
        },
    }


def definition_pbir(semantic_model_folder: str) -> dict[str, Any]:
    """``definition.pbir`` (at the REPORT ROOT) binding report to a model.

    ``version`` is the PBIR definition-properties version ("4.0").
    ``byPath.path`` points at the semantic-MODEL folder (relative path),
    e.g. ``"../MonthlySales.SemanticModel"``.
    """
    return {
        "$schema": _SCHEMA_DEFINITION_PBIR,
        "version": "4.0",
        "datasetReference": {
            "byPath": {"path": semantic_model_folder},
            "byConnection": None,
        },
    }


def definition_pbism() -> dict[str, Any]:
    """``definition.pbism`` for the semantic model.

    'defaultPowerBIDataSourceVersion' is NOT valid in definition.pbism settings
    either (schema rejects additional properties). V3 Enhanced Metadata Format
    is signalled by the PBI_QueryOrder annotation in model.tmdl — not here.
    """
    return {
        "$schema": _SCHEMA_DEFINITION_PBISM,
        "version": "4.2",
        "settings": {},
    }


def pbip_entry(report_folder: str) -> dict[str, Any]:
    """The root ``<name>.pbip`` file Power BI Desktop opens.

    Only ``version``, ``artifacts``, and ``settings`` are permitted by the
    schema (no ``schema``/``$schema`` key -- that trips the validator).
    """
    return {
        "version": "1.0",
        "artifacts": [{"report": {"path": report_folder}}],
        "settings": {"enableAutoRecovery": True},
    }


# ---------------------------------------------------------------------------
# Report definition files
# ---------------------------------------------------------------------------


def version_json() -> dict[str, Any]:
    """``definition/version.json`` -- PBIR format version marker."""
    return {"$schema": _SCHEMA_VERSION, "version": "2.0.0"}


def pages_metadata(page_ids: list[str]) -> dict[str, Any]:
    """``definition/pages/pages.json`` -- page ordering + active page.

    ``page_ids`` must be the internal ids of the page.json files, in order.
    """
    return {
        "$schema": _SCHEMA_PAGES,
        "pageOrder": list(page_ids),
        "activePageName": page_ids[0] if page_ids else None,
    }


def report_json(theme_name: str = "CY24SU10", custom_theme_name: str | None = None) -> dict[str, Any]:
    """Minimal valid ``definition/report.json`` (report/3.0.0 schema).

    Uses the built-in ``CY24SU10`` base theme (shipped with Desktop) so no
    theme resource file is required. If ``custom_theme_name`` is provided, adds
    a ``customTheme`` in themeCollection + resourcePackages to reference theme.json.

    Args:
        theme_name: Base theme name (default ``CY24SU10``).
        custom_theme_name: Optional name of the custom theme from ``theme.json``.
            When set, Power BI Desktop reads ``definition/theme.json`` and applies
            the custom palette. The name must match the ``name`` field in theme.json.
    """
    payload: dict[str, Any] = {
        "$schema": _SCHEMA_REPORT,
        "themeCollection": {
            "baseTheme": {
                "name": theme_name,
                "reportVersionAtImport": {
                    "visual": "1.8.95",
                    "report": "2.0.95",
                    "page": "1.3.95",
                },
                "type": "SharedResources",
            }
        },
        "settings": {
            "useStylableVisualContainerHeader": True,
            "useDefaultAggregateDisplayName": True,
            "useEnhancedTooltips": True,
        },
    }
    if custom_theme_name:
        payload["themeCollection"]["customTheme"] = {
            "name": "theme.json",
            "reportVersionAtImport": {
                "visual": "2.1.0",
                "report": "2.1.0",
                "page": "2.0.0",
            },
            "type": "RegisteredResources",
        }
        payload["resourcePackages"] = [
            {
                "name": "SharedResources",
                "type": "SharedResources",
                "items": [
                    {
                        "name": theme_name,
                        "path": f"BaseThemes/{theme_name}.json",
                        "type": "BaseTheme",
                    }
                ],
            },
            {
                "name": "RegisteredResources",
                "type": "RegisteredResources",
                "items": [
                    {
                        "name": "theme.json",
                        "path": "theme.json",
                        "type": "CustomTheme",
                    }
                ],
            },
        ]
    return payload


def page_json(page_id: str, display_name: str,
              width: int = 1280, height: int = 720) -> dict[str, Any]:
    """``definition/pages/<pageId>/page.json`` (page/2.0.0 schema).

    ``name`` must equal the page id referenced in pages.json; ``displayName``
    is what the user sees. height/width are numbers (not strings).
    """
    return {
        "$schema": _SCHEMA_PAGE,
        "name": page_id,
        "displayName": display_name,
        "displayOption": "FitToPage",
        "height": height,
        "width": width,
    }


# ---------------------------------------------------------------------------
# Visuals
# ---------------------------------------------------------------------------

# Each visual type has a distinct set of queryState roles. These map our
# high-level "what to plot" intent to the PBIR role slots.
_VISUAL_ROLES = {
    # existing
    "card":         ["Values"],
    "barChart":     ["Category", "Y"],
    "columnChart":  ["Category", "Y"],
    "lineChart":    ["Category", "Y"],
    "pieChart":     ["Category", "Y"],
    "tableEx":      ["Values"],
    # Phase 3.1 additions
    "donutChart":   ["Category", "Y"],
    "scatterChart": ["Category", "X", "Y", "Size", "Legend"],
    "matrix":       ["Rows", "Columns", "Values"],
    "kpi":          ["Indicator", "TrendLine", "Goal"],
    "slicer":       ["Values"],
}


def _projection_for_column(table: str, column: str,
                           aggregation: str | None = None) -> dict[str, Any]:
    """A column projection: field.Column + queryRef."""
    query_ref = f"{table}.{column}"
    proj: dict[str, Any] = {
        "field": {
            "Column": {
                "Expression": {"SourceRef": {"Entity": table}},
                "Property": column,
            }
        },
        "queryRef": query_ref,
        "nativeQueryRef": column,
    }
    if aggregation:
        proj["aggregation"] = aggregation
    return proj


def _projection_for_measure(table: str, measure: str) -> dict[str, Any]:
    """A measure projection: field.Measure + queryRef."""
    return {
        "field": {
            "Measure": {
                "Expression": {"SourceRef": {"Entity": table}},
                "Property": measure,
            }
        },
        "queryRef": f"{table}.{measure}",
        "nativeQueryRef": measure,
    }


def _build_query_state(visual_type: str, table: str,
                       fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Map high-level fields into the role slots PBIR expects for this visual.

    ``fields`` is a list of ``{"kind": "column"|"measure", "name": ...,
    "role": <override>}`` entries. Roles are assigned from ``_VISUAL_ROLES``
    in order unless a field specifies an explicit ``role``.
    """
    roles = _VISUAL_ROLES.get(visual_type, ["Values"])
    query_state: dict[str, Any] = {}
    role_idx = 0

    for f in fields:
        role = f.get("role") or (roles[role_idx] if role_idx < len(roles)
                                 else roles[-1])
        role_idx += 1

        proj = (
            _projection_for_measure(table, f["name"])
            if f.get("kind") == "measure"
            else _projection_for_column(
                table, f["name"], f.get("aggregation")
            )
        )
        # each role bucket holds a list of projections
        query_state.setdefault(role, {"projections": []})
        query_state[role]["projections"].append(proj)

    return query_state


def visual_json(visual_id: str, visual_type: str,
                query_state: dict[str, Any],
                x: float = 0.0, y: float = 0.0,
                width: float = 300.0, height: float = 200.0,
                z: int = 0, tab_order: int = 0,
                title: str | None = None) -> dict[str, Any]:
    """Build a ``visual.json`` payload (visualContainer/2.4.0 schema).

    Args:
        visual_id: internal id (must match the folder name).
        visual_type: one of card/barChart/columnChart/lineChart/pieChart/
            tableEx/kpi.
        query_state: the role->projections map from :func:`_build_query_state`.
        x/y/width/height: pixel geometry (floats, like Desktop emits).
        z: z-order (int).
        tab_order: tab order (int).
        title: optional visual title.
    """
    visual: dict[str, Any] = {
        "visualType": visual_type,
        "query": {
            "queryState": query_state,
            "sortDefinition": {"isDefaultSort": True},
        },
    }
    if title:
        visual["objects"] = {
            "title": [
                {
                    "properties": {
                        "show": {"expr": {"Literal": {"Value": "true"}}},
                        "text": {"expr": {"Literal": {"Value": f"'{title}'"}}},
                    }
                }
            ]
        }

    return {
        "$schema": _SCHEMA_VISUAL,
        "name": visual_id,
        "position": {
            "x": float(x),
            "y": float(y),
            "z": int(z),
            "height": float(height),
            "width": float(width),
            "tabOrder": int(tab_order),
        },
        "visual": visual,
    }


def build_visual(visual_id: str, visual_type: str, table: str,
                 fields: list[dict[str, Any]], **geom: Any) -> dict[str, Any]:
    """Convenience: build a complete visual from high-level field specs.

    ``fields`` example: ``[{"kind":"measure","name":"Total Sales"}]`` for a
    card, or ``[{"kind":"column","name":"Region"},
    {"kind":"measure","name":"Total Sales"}]`` for a bar chart.
    """
    qs = _build_query_state(visual_type, table, fields)
    return visual_json(visual_id, visual_type, qs, **geom)


# ---------------------------------------------------------------------------
# High-level convenience builders  (single source of truth for ReportAgent)
# ---------------------------------------------------------------------------

def _pos_dict(pos: dict[str, Any]) -> dict[str, Any]:
    """Normalise a position dict: accept tabOrder OR tab_order, coerce types."""
    return {
        "x": float(pos.get("x", 0)),
        "y": float(pos.get("y", 0)),
        "z": int(pos.get("z", 0)),
        "height": float(pos.get("height", 200)),
        "width": float(pos.get("width", 300)),
        "tabOrder": int(pos.get("tabOrder", pos.get("tab_order", 0))),
    }


def _make_visual(visual_id: str, visual_type: str,
                 pos: dict[str, Any],
                 query_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "$schema": _SCHEMA_VISUAL,
        "name": visual_id,
        "position": _pos_dict(pos),
        "visual": {
            "visualType": visual_type,
            "query": {
                "queryState": query_state,
                "sortDefinition": {"isDefaultSort": True},
            },
            "drillFilterOtherVisuals": True,
        },
    }


def build_card(visual_id: str, pos: dict[str, Any],
               table: str, measure: str) -> dict[str, Any]:
    """Card visual showing a single measure."""
    return _make_visual(visual_id, "card", pos,
                        {"Values": {"projections": [
                            _projection_for_measure(table, measure)
                        ]}})


def build_bar_chart(visual_id: str, pos: dict[str, Any],
                    table: str, category: str, measure: str) -> dict[str, Any]:
    """Horizontal bar chart: category on axis, measure on value."""
    return _make_visual(visual_id, "barChart", pos, {
        "Category": {"projections": [_projection_for_column(table, category)]},
        "Y": {"projections": [_projection_for_measure(table, measure)]},
    })


def build_column_chart(visual_id: str, pos: dict[str, Any],
                       table: str, category: str, measure: str) -> dict[str, Any]:
    """Vertical column chart: category on axis, measure on value."""
    return _make_visual(visual_id, "columnChart", pos, {
        "Category": {"projections": [_projection_for_column(table, category)]},
        "Y": {"projections": [_projection_for_measure(table, measure)]},
    })


def build_line_chart(visual_id: str, pos: dict[str, Any],
                     table: str, category: str, measure: str) -> dict[str, Any]:
    """Line chart: category on x-axis, measure on y."""
    return _make_visual(visual_id, "lineChart", pos, {
        "Category": {"projections": [_projection_for_column(table, category)]},
        "Y": {"projections": [_projection_for_measure(table, measure)]},
    })


def build_table(visual_id: str, pos: dict[str, Any],
                column_defs: list[tuple[str, str]]) -> dict[str, Any]:
    """Table (tableEx) visual with the given (table, column/measure) pairs.

    column_defs: list of (table, column_or_measure_name, kind)
    kind is "column" (default) or "measure".
    """
    projections = []
    for entry in column_defs:
        t, name = entry[0], entry[1]
        kind = entry[2] if len(entry) > 2 else "column"
        if kind == "measure":
            projections.append(_projection_for_measure(t, name))
        else:
            projections.append(_projection_for_column(t, name))
    return _make_visual(visual_id, "tableEx", pos,
                        {"Values": {"projections": projections}})


def build_matrix(visual_id: str, pos: dict[str, Any],
                 table: str,
                 rows: list[str],
                 values: list[str],
                 columns: list[str] | None = None) -> dict[str, Any]:
    """Matrix visual: row hierarchy, optional column hierarchy, value measures."""
    qs: dict[str, Any] = {
        "Rows":   {"projections": [_projection_for_column(table, r) for r in rows]},
        "Values": {"projections": [_projection_for_measure(table, v) for v in values]},
    }
    if columns:
        qs["Columns"] = {"projections": [_projection_for_column(table, c) for c in columns]}
    return _make_visual(visual_id, "matrix", pos, qs)


def build_donut(visual_id: str, pos: dict[str, Any],
                table: str, category: str, measure: str) -> dict[str, Any]:
    """Donut chart: category slices, measure values."""
    return _make_visual(visual_id, "donutChart", pos, {
        "Category": {"projections": [_projection_for_column(table, category)]},
        "Y":        {"projections": [_projection_for_measure(table, measure)]},
    })


def build_pie(visual_id: str, pos: dict[str, Any],
             table: str, category: str, measure: str) -> dict[str, Any]:
    """Pie chart: category slices, measure values (same query shape as donut)."""
    return _make_visual(visual_id, "pieChart", pos, {
        "Category": {"projections": [_projection_for_column(table, category)]},
        "Y":        {"projections": [_projection_for_measure(table, measure)]},
    })


def build_scatter(visual_id: str, pos: dict[str, Any],
                  table: str,
                  x_measure: str,
                  y_measure: str,
                  details: str | None = None,
                  legend: str | None = None) -> dict[str, Any]:
    """Scatter chart: category dimension + X/Y measures (+ optional Size, Legend)."""
    qs: dict[str, Any] = {
        "X": {"projections": [_projection_for_measure(table, x_measure)]},
        "Y": {"projections": [_projection_for_measure(table, y_measure)]},
    }
    if details:
        qs["Category"] = {"projections": [_projection_for_column(table, details)]}
    if legend:
        qs["Legend"] = {"projections": [_projection_for_column(table, legend)]}
    return _make_visual(visual_id, "scatterChart", pos, qs)


def build_kpi(visual_id: str, pos: dict[str, Any],
              table: str,
              indicator: str,
              trend_axis: str | None = None,
              goal: str | None = None) -> dict[str, Any]:
    """KPI visual: indicator measure, optional trend axis column, optional goal."""
    qs: dict[str, Any] = {
        "Indicator": {"projections": [_projection_for_measure(table, indicator)]},
    }
    if trend_axis:
        qs["TrendLine"] = {"projections": [_projection_for_column(table, trend_axis)]}
    if goal:
        qs["Goal"] = {"projections": [_projection_for_measure(table, goal)]}
    return _make_visual(visual_id, "kpi", pos, qs)


def build_slicer(visual_id: str, pos: dict[str, Any],
                 table: str, field: str,
                 kind: str = "column") -> dict[str, Any]:
    """Slicer visual on a column or measure field."""
    proj = (_projection_for_column(table, field) if kind == "column"
            else _projection_for_measure(table, field))
    return _make_visual(visual_id, "slicer", pos,
                        {"Values": {"projections": [proj]}})


# ---------------------------------------------------------------------------
# Conditional formatting helpers
# ---------------------------------------------------------------------------


def apply_color_scale(visual: dict[str, Any],
                      cf_rule: dict[str, Any]) -> dict[str, Any]:
    """Attach a color-scale rule to a chart visual's dataPoint formatting.

    Mutates ``visual`` in place and returns it for chaining. Use with the
    output of :func:`patterns.conditional_formatting.color_scale_2` or
    :func:`color_scale_3`.

    Works on chart types: barChart, columnChart, lineChart, scatterChart,
    donutChart, pieChart, areaChart, etc.
    """
    objects = visual["visual"].setdefault("objects", {})
    objects.setdefault("dataPoint", []).append(cf_rule)
    return visual


def apply_data_bars(visual: dict[str, Any],
                    cf_rule: dict[str, Any]) -> dict[str, Any]:
    """Attach a data-bars rule to a table/matrix visual.

    Mutates ``visual`` in place and returns it for chaining. Use with the
    output of :func:`patterns.conditional_formatting.data_bars`.

    Only works on tableEx and matrix visuals.
    """
    objects = visual["visual"].setdefault("objects", {})
    objects.setdefault("columnFormatting", []).append(cf_rule)
    return visual


def apply_icon_set(visual: dict[str, Any],
                   cf_rule: dict[str, Any]) -> dict[str, Any]:
    """Attach an icon-set rule to a table/matrix visual.

    Mutates ``visual`` in place and returns it for chaining. Use with the
    output of :func:`patterns.conditional_formatting.icon_set`.

    Only works on tableEx and matrix visuals.
    """
    objects = visual["visual"].setdefault("objects", {})
    objects.setdefault("values", []).append(cf_rule)
    return visual


# ---------------------------------------------------------------------------
# Deneb (Vega-Lite custom visual) builder
# ---------------------------------------------------------------------------

# The custom visual identifier Deneb is always registered under in Power BI.
# DO NOT change — it is the public visual GUID used by every Deneb-enabled
# report that bundles the visual via the marketplace.
DENEB_VISUAL_TYPE = "deneb7E15AEF80B9E4D4F8E12924291ECE89A"

# Default Vega config — light, Segoe UI, fit autosize.
_DENEB_DEFAULT_CONFIG = {
    "autosize": {"type": "fit", "contains": "padding"},
    "view": {"stroke": "transparent"},
    "font": "Segoe UI",
}


def _deneb_literal(value: str) -> dict[str, Any]:
    """Wrap a string in the PBIR Literal/Value envelope expected by Deneb."""
    return {"expr": {"Literal": {"Value": value}}}


def _deneb_string_literal(text: str) -> dict[str, Any]:
    """Quote a string so the Vega editor reads it as a literal."""
    # PBIR string literals use single quotes around the JSON payload, and any
    # single quote inside the payload must be doubled (the same rule the
    # reference KPI/Bullet examples follow).
    escaped = text.replace("'", "''")
    return _deneb_literal(f"'{escaped}'")


def _deneb_bool_literal(value: bool) -> dict[str, Any]:
    return _deneb_literal("true" if value else "false")


def _deneb_double_literal(value: float | int) -> dict[str, Any]:
    # PBIR encodes numeric doubles with a trailing ``D``.
    if float(value).is_integer():
        return _deneb_literal(f"{int(value)}D")
    return _deneb_literal(f"{value}D")


def _deneb_query_state(table: str,
                       fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the queryState.dataset.projections list Deneb expects.

    Deneb uses a flat ``dataset`` role (not Category/Value buckets) — every
    bound field is projected into ``queryState.dataset.projections``.
    """
    projections = []
    for f in fields:
        proj = (_projection_for_measure(table, f["name"])
                if f.get("kind") == "measure"
                else _projection_for_column(table, f["name"],
                                            f.get("aggregation")))
        projections.append(proj)
    return {"dataset": {"projections": projections}}


def build_deneb_visual(
    visual_id: str,
    pos: dict[str, Any],
    table: str,
    fields: list[dict[str, Any]],
    vega_lite_spec: dict[str, Any],
    config: dict[str, Any] | None = None,
    enable_tooltips: bool = True,
    enable_context_menu: bool = True,
    enable_highlight: bool = True,
    enable_selection: bool = True,
    selection_max_data_points: int = 50,
    vega_version: str = "5.20.1",
    deneb_version: str = "1.7.1.0",
) -> dict[str, Any]:
    """Build a Deneb (Vega-Lite) custom-visual container.

    Args:
        visual_id: internal id (must match the folder name).
        pos: position dict (``x``, ``y``, ``width``, ``height``, optional ``z``,
            ``tabOrder``).
        table: source entity name that owns the projected fields.
        fields: list of ``{"kind": "column"|"measure", "name": <name>}``. Each
            entry becomes a projection in ``queryState.dataset.projections``.
            The names also appear as field references inside the Vega-Lite
            spec (e.g. ``datum['Total Sales']``).
        vega_lite_spec: a Vega-Lite v5 spec dict. Use any of the presets in
            :mod:`patterns.deneb` or supply a hand-written spec.
        config: optional Vega config dict; defaults to a light Segoe UI config.
        enable_*: cross-filter / interaction flags (defaults match the
            reference Deneb visual).
        selection_max_data_points: limit Power BI passes to Deneb when
            selection mode is on.
        vega_version: Vega/Vega-Lite runtime version Deneb embeds.
        deneb_version: the Deneb custom-visual version installed.
    """
    import json

    spec_str = json.dumps(vega_lite_spec, separators=(", ", ": "))
    cfg_str = json.dumps(config or _DENEB_DEFAULT_CONFIG,
                         separators=(",", ":"))

    deneb_objects = {
        "vega": [
            {
                "properties": {
                    "provider": _deneb_string_literal("vegaLite"),
                    "jsonSpec": _deneb_string_literal(spec_str),
                    "enableTooltips": _deneb_bool_literal(enable_tooltips),
                    "jsonConfig": _deneb_string_literal(cfg_str),
                    "isNewDialogOpen": _deneb_bool_literal(False),
                    "enableContextMenu":
                        _deneb_bool_literal(enable_context_menu),
                    "enableHighlight": _deneb_bool_literal(enable_highlight),
                    "enableSelection": _deneb_bool_literal(enable_selection),
                    "selectionMaxDataPoints":
                        _deneb_double_literal(selection_max_data_points),
                    "logLevel": _deneb_double_literal(3),
                    "version": _deneb_string_literal(vega_version),
                }
            }
        ],
        "stateManagement": [
            {
                "properties": {
                    "viewportHeight":
                        _deneb_double_literal(int(pos.get("height", 200)) - 10),
                    "viewportWidth":
                        _deneb_double_literal(int(pos.get("width", 400)) - 10),
                }
            }
        ],
        "editor": [
            {
                "properties": {
                    "fontSize": _deneb_double_literal(8),
                    "wordWrap": _deneb_bool_literal(True),
                    "theme": _deneb_string_literal("light"),
                }
            }
        ],
        "developer": [
            {
                "properties": {
                    "version": _deneb_string_literal(deneb_version),
                }
            }
        ],
    }

    return {
        "$schema": _SCHEMA_VISUAL,
        "name": visual_id,
        "position": _pos_dict(pos),
        "visual": {
            "visualType": DENEB_VISUAL_TYPE,
            "query": {
                "queryState": _deneb_query_state(table, fields),
                "sortDefinition": {"isDefaultSort": True},
            },
            "objects": deneb_objects,
            "drillFilterOtherVisuals": True,
        },
    }
