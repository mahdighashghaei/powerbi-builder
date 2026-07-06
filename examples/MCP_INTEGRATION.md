# MCP Integration — Claude Desktop

The `powerbi-builder` MCP server exposes **15 tools** that any MCP client
(Claude Desktop, OpenAI Studio, custom LLM agent, …) can call to build, edit,
and deploy Power BI projects.

## Quick start (Claude Desktop)

1. Install the MCP SDK in this project's Python environment:

    ```bash
    pip install mcp
    ```

2. Copy [`examples/claude_desktop_config.template.json`](claude_desktop_config.template.json)
   into the Claude Desktop config file:

    - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
    - macOS:   `~/Library/Application Support/Claude/claude_desktop_config.json`
    - Linux:   `~/.config/Claude/claude_desktop_config.json`

3. Replace `<ABS_PATH>` with the absolute path to this repository folder and
   `<PYTHON>` with the absolute path to your Python 3.10+ interpreter.

4. Restart Claude Desktop. The 15 tools should appear in the **🔌 plug-in
   menu** under the `powerbi-builder` server.

## Available tools

### Low-level (file-level operations)

| Tool                       | Purpose |
|----------------------------|---------|
| `read_csv_schema`          | Infer the Power BI schema from a CSV/JSON/Excel file. |
| `write_tmdl_table`         | Write a TMDL table definition. |
| `write_tmdl_measures`      | Append DAX measures to a TMDL table file. |
| `write_pbir_page`          | Write a PBIR page + its visuals. |
| `write_deneb_visual`       | Add a Deneb (Vega-Lite) visual to a page. |
| `write_theme_json`         | Write the report theme. |
| `validate_pbip_structure`  | Validate a .pbip folder's structure. |

### High-level (one-shot orchestration)

| Tool                | Purpose |
|---------------------|---------|
| `generate_pbip`     | Build a complete PBIP from a CSV/Excel/JSON file. |
| `edit_pbip`         | Edit an existing PBIP from a plain-English description. |
| `add_measure`       | Append a single DAX measure to an existing PBIP. |
| `add_visual`        | Add one visual to an existing page. |
| `add_page`          | Add a new page (with optional visuals) + update pages.json. |
| `deploy_to_fabric`  | Upload to a Fabric workspace (defaults to `dry_run=True`). |
| `suggest_measures`  | Propose DAX measures from a file or PBIP project. |

## Example conversations

```
You:    Build a sales dashboard from C:/data/sales.csv
Claude: [calls generate_pbip] — created SalesDashboard.pbip with 5 measures, 1 page.

You:    Add a YoY % measure
Claude: [calls add_measure] — added "Total Sales YoY %" to the Sales table.

You:    Add a trend page with a line chart of Total Sales by Date
Claude: [calls add_page with visuals=[…]] — added page "Trend".

You:    Deploy to my workspace "Sales Team" (dry-run first)
Claude: [calls deploy_to_fabric with dry_run=True] — fab import Sales Team.Workspace/SalesDashboard.SemanticModel …
You:    Looks good — do it for real.
Claude: [calls deploy_to_fabric with dry_run=False] — Uploaded.
```

## Security

- The MCP server only writes inside the `output_root` passed as its argv
  (path-traversal guard via `utils.security.safe_join`).
- Every tool call is audit-logged to `logs/mcp_server.log`.
- `deploy_to_fabric` defaults to `dry_run=True`; the LLM has to explicitly
  set it to `False` to publish.
- The Fabric CLI requires its own auth (`fab auth login`); the MCP server
  does not store any credentials.

## Troubleshooting

- **"mcp module not found"**: `pip install mcp` in the same Python you point at.
- **Tools missing in the plug-in menu**: check `logs/mcp_server.log` for tracebacks.
- **Path errors on Windows**: use forward slashes in JSON paths or escape
  backslashes (`"C:\\\\Users\\\\…"`).
