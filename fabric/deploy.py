"""High-level PBIP deployment to Microsoft Fabric.

Given a local PBIP directory (output of ``create_project_scaffold`` +
write tools), this module:

  1. Locates the ``*.SemanticModel`` and ``*.Report`` folders.
  2. Runs a pre-flight check (structure validation + optional BPA gate).
  3. Decides whether to ``create`` or ``update`` each item based on what
     the workspace already contains.
  4. Calls ``fab import`` for the semantic model first, then the report.
  5. Returns a structured result with everything that happened.

The order matters: a Report references a SemanticModel by ID via its
``definition.pbir`` ``byPath`` reference, so the model must exist in the
workspace before the report is published.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import fab_cli

log = logging.getLogger("powerbi_builder.fabric.deploy")


# ---------------------------------------------------------------------------
# Pre-flight: locate folders + sanity check
# ---------------------------------------------------------------------------

@dataclass
class PbipLayout:
    """Resolved on-disk layout of a PBIP project."""
    pbip_root:           Path
    semantic_model_dir:  Path
    semantic_model_name: str
    report_dir:          Path
    report_name:         str

    def as_dict(self) -> dict[str, str]:
        return {
            "pbip_root":           str(self.pbip_root),
            "semantic_model_dir":  str(self.semantic_model_dir),
            "semantic_model_name": self.semantic_model_name,
            "report_dir":          str(self.report_dir),
            "report_name":         self.report_name,
        }


def resolve_layout(pbip_dir: str | Path) -> PbipLayout:
    """Find the SemanticModel + Report folders under ``pbip_dir``.

    Accepts either the parent folder (``output/``) when there's only one
    project, OR the actual project root containing both ``*.SemanticModel``
    and ``*.Report`` subfolders.
    """
    root = Path(pbip_dir).resolve()
    if not root.exists():
        raise FileNotFoundError(f"PBIP path not found: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"PBIP path is not a directory: {root}")

    sm_dirs = sorted(root.glob("*.SemanticModel"))
    rp_dirs = sorted(root.glob("*.Report"))

    if not sm_dirs:
        raise FileNotFoundError(f"No *.SemanticModel folder under {root}")
    if not rp_dirs:
        raise FileNotFoundError(f"No *.Report folder under {root}")
    if len(sm_dirs) > 1 or len(rp_dirs) > 1:
        raise ValueError(
            f"Multiple PBIP projects under {root}; pass a specific project "
            f"root instead. Found models={[d.name for d in sm_dirs]} "
            f"reports={[d.name for d in rp_dirs]}"
        )

    sm = sm_dirs[0]
    rp = rp_dirs[0]
    return PbipLayout(
        pbip_root=root,
        semantic_model_dir=sm,
        semantic_model_name=sm.name[: -len(".SemanticModel")],
        report_dir=rp,
        report_name=rp.name[: -len(".Report")],
    )


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------

@dataclass
class DeployResult:
    """Outcome of a deploy invocation."""
    ok:           bool
    pbip:         dict[str, str] = field(default_factory=dict)
    workspace:    str = ""
    mode:         str = ""              # "create" | "update" | "auto"
    dry_run:      bool = False
    actions:      list[dict[str, Any]] = field(default_factory=list)
    error:        str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok":        self.ok,
            "pbip":      self.pbip,
            "workspace": self.workspace,
            "mode":      self.mode,
            "dry_run":   self.dry_run,
            "actions":   self.actions,
            "error":     self.error,
        }


def _decide_action(mode: str, exists: bool) -> str:
    if mode == "create":
        return "create"
    if mode == "update":
        return "update"
    # auto
    return "update" if exists else "create"


def deploy(
    pbip_dir: str | Path,
    workspace: str,
    *,
    mode: str = "auto",
    dry_run: bool = False,
    skip_report: bool = False,
    skip_model: bool = False,
) -> DeployResult:
    """Deploy a PBIP project to a Fabric workspace.

    Args:
        pbip_dir:    path that contains a ``*.SemanticModel`` and a ``*.Report``
                     subfolder. Typically the output of the create pipeline.
        workspace:   workspace NAME (not ID). The CLI resolves the name to the
                     ``<name>.Workspace`` virtual path.
        mode:        ``"auto"`` (default), ``"create"``, or ``"update"``.
                     auto = update if the named item already exists, else create.
        dry_run:     when True, log every fab command but never invoke it.
        skip_report / skip_model: deploy only one side of the project.

    Returns:
        DeployResult — inspect ``.actions`` for per-item outcomes.
    """
    try:
        layout = resolve_layout(pbip_dir)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        return DeployResult(ok=False, workspace=workspace, mode=mode,
                            dry_run=dry_run, error=str(exc))

    if mode not in {"auto", "create", "update"}:
        return DeployResult(ok=False, pbip=layout.as_dict(),
                            workspace=workspace, mode=mode, dry_run=dry_run,
                            error=f"invalid mode '{mode}' (auto/create/update)")

    # Optional pre-flight: confirm fab is installed + signed-in
    if not dry_run and not fab_cli.is_installed():
        return DeployResult(
            ok=False, pbip=layout.as_dict(), workspace=workspace,
            mode=mode, dry_run=False,
            error="fab CLI not installed. pip install ms-fabric-cli",
        )

    actions: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 1) SemanticModel first
    # ------------------------------------------------------------------
    if not skip_model:
        sm_exists = (
            False if dry_run
            else fab_cli.item_exists(workspace, layout.semantic_model_name,
                                     "SemanticModel")
        )
        sm_action = _decide_action(mode, sm_exists)
        log.info(
            "deploy %s '%s' -> %s.Workspace (action=%s)",
            "SemanticModel", layout.semantic_model_name, workspace, sm_action,
        )
        sm_result = fab_cli.import_item(
            workspace_name=workspace,
            item_name=layout.semantic_model_name,
            item_type="SemanticModel",
            local_path=layout.semantic_model_dir,
            force=True,
            dry_run=dry_run,
        )
        actions.append({
            "kind":   "SemanticModel",
            "name":   layout.semantic_model_name,
            "action": sm_action,
            "result": sm_result.as_dict(),
        })
        if not sm_result.ok:
            return DeployResult(
                ok=False, pbip=layout.as_dict(), workspace=workspace,
                mode=mode, dry_run=dry_run, actions=actions,
                error=f"SemanticModel deploy failed: {sm_result.error}",
            )

    # ------------------------------------------------------------------
    # 2) Report (depends on the model existing)
    # ------------------------------------------------------------------
    if not skip_report:
        rp_exists = (
            False if dry_run
            else fab_cli.item_exists(workspace, layout.report_name, "Report")
        )
        rp_action = _decide_action(mode, rp_exists)
        log.info(
            "deploy Report '%s' -> %s.Workspace (action=%s)",
            layout.report_name, workspace, rp_action,
        )
        rp_result = fab_cli.import_item(
            workspace_name=workspace,
            item_name=layout.report_name,
            item_type="Report",
            local_path=layout.report_dir,
            force=True,
            dry_run=dry_run,
        )
        actions.append({
            "kind":   "Report",
            "name":   layout.report_name,
            "action": rp_action,
            "result": rp_result.as_dict(),
        })
        if not rp_result.ok:
            return DeployResult(
                ok=False, pbip=layout.as_dict(), workspace=workspace,
                mode=mode, dry_run=dry_run, actions=actions,
                error=f"Report deploy failed: {rp_result.error}",
            )

    return DeployResult(
        ok=True, pbip=layout.as_dict(), workspace=workspace,
        mode=mode, dry_run=dry_run, actions=actions,
    )


__all__ = ["PbipLayout", "DeployResult", "resolve_layout", "deploy"]
