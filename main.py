"""PowerBI Builder -- single-command entry point.

Usage::

    python main.py --csv examples/sample.csv --desc "Monthly sales by region"
    python main.py --schema schema.json --desc "Customer churn dashboard" \\
                   --project-name ChurnDashboard

What it does
------------
1. Loads configuration from the environment / ``.env`` (no hardcoded secrets).
2. Builds an :class:`OrchestratorAgent` which runs the full pipeline:
   SchemaAgent -> DAXAgent -> ReportAgent -> ValidatorAgent.
3. Prints a concise summary and exits with a non-zero code on failure.

The orchestrator writes the .pbip folder (SemanticModel + Report) under the
configured output directory (``OUTPUT_DIR``, default ``./output``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ensure the project root is importable when run as `python main.py`
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agents.orchestrator import OrchestratorAgent, RunReport  # noqa: E402
from config import load_settings  # noqa: E402
from utils import AuditLogger  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="powerbi-builder",
        description=(
            "Build or edit a Power BI Project (.pbip) using AI.\n\n"
            "Modes:\n"
            "  --csv / --schema   Create a new PBIP from data\n"
            "  --pbip             Edit an existing PBIP folder\n"
            "  --pbix             Edit an existing PBIX file\n"
            "  --excel            Build from an Excel file\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv",    metavar="PATH", help="Input CSV file (create mode).")
    src.add_argument("--schema", metavar="PATH", help="Input JSON schema file (create mode).")
    src.add_argument("--pbip",   metavar="PATH", help="Existing PBIP folder to edit.")
    src.add_argument("--pbix",   metavar="PATH", help="Existing PBIX file to edit.")
    src.add_argument("--excel",  metavar="PATH", help="Excel file to build from.")

    p.add_argument("--sheet", default=None,
                   help="Excel sheet name (default: first sheet). Used with --excel.")
    p.add_argument("--desc", required=True, help="Plain-English description of what to build/add.")
    p.add_argument("--project-name", default=None,
                   help="Override the auto-derived project (folder) name.")
    p.add_argument("--theme", default="default",
                   help="Theme preset: default, modern_dark, corporate_blue, earth_tones, vibrant (default: default).")
    p.add_argument("--output-dir", default=None,
                   help="Override the output directory (default: ./output).")
    p.add_argument("--log-file", default=None,
                   help="Override the audit log file path.")
    p.add_argument("--log-level", default=None, help="Override the log level.")
    p.add_argument("--report-json", action="store_true",
                   help="Also write a machine-readable run report to stdout.")
    p.add_argument("--deploy-to", metavar="WORKSPACE", default=None,
                   help="After a successful build, deploy the PBIP to this "
                        "Fabric workspace (by name). Requires the `fab` CLI "
                        "(`pip install ms-fabric-cli`) and a prior `fab auth login`.")
    p.add_argument("--deploy-mode", choices=["auto", "create", "update"], default="auto",
                   help="Behaviour when an item with the same name already exists "
                        "(default: auto = update if exists else create).")
    p.add_argument("--deploy-dry-run", action="store_true",
                   help="Print the fab commands that would run, but do not invoke them.")
    p.add_argument("--interactive", action="store_true",
                   help="Interactive mode: pause and ask the user when the Data "
                        "Analyzer has ambiguous questions (e.g. a column with "
                        "50%% nulls). Without this flag, best-effort decisions "
                        "are applied automatically with a warning.")
    return p.parse_args(argv)


def _print_summary(report: RunReport) -> None:
    print()
    print("=" * 60)
    status = "[OK] SUCCESS" if report.ok else "[FAIL] FAILED"
    print(f"{status}: {report.project_name}")
    print(f"  output: {report.pbip_root}")
    print("-" * 60)
    for step in report.steps:
        mark = "[OK]" if step["ok"] else "[FAIL]"
        print(f"  {mark} {step['agent']:<16} {step['message']}")
        for err in step["errors"]:
            print(f"      • {err}")
    if report.validation:
        v = report.validation
        print("-" * 60)
        print(f"  tables={v.get('tables',0)} measures={v.get('measures',0)} "
              f"pages={v.get('pages',0)} visuals={v.get('visuals',0)}")
        if v.get("fixes_applied"):
            print(f"  auto-fixes: {len(v['fixes_applied'])}")
        if v.get("warnings"):
            print(f"  warnings: {len(v['warnings'])}")
    print("=" * 60)
    if report.ok:
        print("Open the .Report folder in Power BI Desktop.")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = load_settings()

    # CLI overrides take precedence over env/.env
    output_dir = args.output_dir or str(settings.output_dir)
    log_file = args.log_file or (str(settings.log_file) if settings.log_file else None)
    log_level = (args.log_level or settings.log_level).upper()

    # configure logging centrally before anything runs
    AuditLogger.configure(log_file=log_file, level=log_level)

    # determine input mode + source path
    if args.pbip:
        input_mode = "edit_pbip"
        source = args.pbip
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_dir():
            print(f"ERROR: PBIP folder not found: {source_path}", file=sys.stderr)
            return 2
    elif args.pbix:
        input_mode = "edit_pbix"
        source = args.pbix
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file():
            print(f"ERROR: PBIX file not found: {source_path}", file=sys.stderr)
            return 2
    elif args.excel:
        input_mode = "edit_excel"
        source = args.excel
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file():
            print(f"ERROR: Excel file not found: {source_path}", file=sys.stderr)
            return 2
    else:
        input_mode = "create"
        source = args.csv or args.schema
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file():
            print(f"ERROR: input file not found: {source_path}", file=sys.stderr)
            return 2

    orchestrator = OrchestratorAgent(
        output_root=output_dir, log_file=log_file, log_level=log_level
    )
    report = orchestrator.run(
        source_path=source_path,
        business_description=args.desc,
        project_name=args.project_name,
        input_mode=input_mode,
        theme_preset=args.theme,
        sheet=args.sheet,
        interactive=args.interactive,
    )

    _print_summary(report)

    if args.report_json:
        print(json.dumps(report.as_dict(), indent=2))

    # ------------------------------------------------------------------
    # Optional: deploy to Microsoft Fabric (Phase 6)
    # ------------------------------------------------------------------
    if args.deploy_to and report.ok:
        try:
            from fabric.deploy import deploy as fabric_deploy
        except ImportError as exc:
            print(f"[deploy] Fabric module unavailable: {exc}", file=sys.stderr)
            return 0

        print()
        print("=" * 60)
        print(f"[deploy] target workspace: {args.deploy_to}  "
              f"(mode={args.deploy_mode}, dry_run={args.deploy_dry_run})")
        print("=" * 60)
        result = fabric_deploy(
            pbip_dir=report.pbip_root,
            workspace=args.deploy_to,
            mode=args.deploy_mode,
            dry_run=args.deploy_dry_run,
        )
        for a in result.actions:
            mark = "[OK]" if a["result"].get("ok") else "[FAIL]"
            print(f"  {mark} {a['kind']:<14} '{a['name']}' "
                  f"action={a['action']}")
            err = a["result"].get("error", "")
            if err:
                print(f"      • {err}")
        if not result.ok:
            print(f"[deploy] FAILED: {result.error}", file=sys.stderr)
            return 3
        if result.dry_run:
            print("[deploy] dry-run complete — pass without --deploy-dry-run to publish.")
        else:
            print("[deploy] SUCCESS — items uploaded.")

    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
