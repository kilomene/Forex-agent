#!/usr/bin/env python3
"""Build a candidate version dir from a proposal.

versions/<version-id>/ gets:
  files/          the changed files (relative layout of the bridge)
  version.json    manifest (id, parent, summary, tier, change_class, ...)
  proposal.md     copy of the proposal for the record

The proposal's patch.diff must apply cleanly to the CURRENT live tree
(dry-run first); otherwise the version is refused before any gate runs.
"""

import os
import re
import shutil
import subprocess

from . import lib
from . import manifest


def read_proposal_header(bridge, proposal_id):
    p = os.path.join(bridge, "proposals", proposal_id, "proposal.md")
    header = {}
    try:
        with open(p) as f:
            lines = f.read().splitlines()
    except OSError:
        raise lib.PipelineHalt(f"proposal {proposal_id}: proposal.md missing")
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if ":" in line:
                k, v = line.split(":", 1)
                header[k.strip()] = v.strip()
    header.setdefault("id", proposal_id)
    return header


def _slug(s):
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:48] or "version"


def _have_patch_tool():
    return shutil.which("patch") is not None


def prepare_from_proposal(proposal_id, bridge=None, summary_override=None):
    """Returns the new version id."""
    bridge = lib.bridge_root(bridge)
    pdir = os.path.join(bridge, "proposals", proposal_id)
    if not os.path.isdir(pdir):
        raise lib.PipelineHalt(f"proposal {proposal_id} not found")
    header = read_proposal_header(bridge, proposal_id)
    tier = header.get("tier", "auto")
    if tier not in ("auto", "zenas"):
        raise lib.PipelineHalt(
            f"proposal {proposal_id}: bad tier {tier!r}")
    # Tier "zenas" is hard-blocked: the pipeline never installs
    # (a) kill-switch resumes or (b) blocked_symbols changes -- those
    # are Zenas's own actions, per his governing 2026-10-18 order.
    if tier == "zenas":
        raise lib.PipelineError(
            f"proposal {proposal_id}: tier 'zenas' is hard-blocked -- the "
            f"pipeline never installs (a) kill-switch resumes or (b) "
            f"blocked_symbols changes; refused")
    change_class = header.get("change_class", "refactor")
    if change_class not in ("refactor", "behavior"):
        raise lib.PipelineHalt(
            f"proposal {proposal_id}: bad change_class {change_class!r}")

    patch_path = os.path.join(pdir, "patch.diff")
    if not os.path.exists(patch_path):
        raise lib.PipelineHalt(
            f"proposal {proposal_id}: patch.diff missing")
    if not _have_patch_tool():
        raise lib.PipelineHalt("the `patch` tool is not installed")

    stamp = lib.utc_now_iso().replace("-", "").replace(":", "")
    version_id = f"v{stamp}-{_slug(proposal_id)}"
    vdir = lib.version_dir(version_id, bridge)
    scratch = os.path.join(vdir, "_scratch")
    os.makedirs(vdir, exist_ok=True)

    # Scratch = live tree, then dry-run + apply the patch, then tests.
    lib.snapshot_tree(bridge, scratch)
    dry = subprocess.run(
        ["patch", "-p1", "--dry-run", "--silent", "-i", patch_path],
        cwd=scratch, capture_output=True, text=True)
    if dry.returncode != 0:
        shutil.rmtree(vdir, ignore_errors=True)
        raise lib.PipelineHalt(
            f"proposal {proposal_id}: patch.diff does not apply cleanly: "
            f"{dry.stdout[-800:]} {dry.stderr[-800:]}")
    app = subprocess.run(
        ["patch", "-p1", "--silent", "-i", patch_path],
        cwd=scratch, capture_output=True, text=True)
    if app.returncode != 0:
        shutil.rmtree(vdir, ignore_errors=True)
        raise lib.PipelineHalt(
            f"proposal {proposal_id}: patch apply failed: "
            f"{app.stderr[-800:]}")

    # Proposal tests ride along.
    tdir = os.path.join(pdir, "tests")
    if os.path.isdir(tdir):
        for fn in sorted(os.listdir(tdir)):
            if fn.startswith("test_") and fn.endswith(".py"):
                shutil.copy2(os.path.join(tdir, fn),
                             os.path.join(scratch, "tests", fn))

    # Changed files = scratch minus live.
    files_dir = os.path.join(vdir, "files")
    os.makedirs(files_dir, exist_ok=True)
    changed = []
    for rel, scratch_path in lib.iter_code_files(scratch):
        if rel.startswith("_scratch/"):
            continue
        live_path = os.path.join(bridge, rel)
        new = (not os.path.exists(live_path)
               or lib.sha256_file(scratch_path)
               != lib.sha256_file(live_path))
        if new:
            dest = os.path.join(files_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(scratch_path, dest)
            changed.append(rel)
    shutil.rmtree(scratch, ignore_errors=True)
    if not changed:
        shutil.rmtree(vdir, ignore_errors=True)
        raise lib.PipelineHalt(
            f"proposal {proposal_id}: patch produced no changes")

    shutil.copy2(os.path.join(pdir, "proposal.md"),
                 os.path.join(vdir, "proposal.md"))
    parent = manifest.read_installed(bridge).get("current")
    man = manifest.new_manifest(
        version_id, parent=parent,
        change_summary=summary_override or header.get(
            "summary", f"proposal {proposal_id}"),
        proposal_id=proposal_id, tier=tier,
        change_class=change_class, files=changed)
    manifest.write_version(man, bridge)
    lib.log_line(f"[prepare] version {version_id}: {len(changed)} files "
                 f"(tier={tier}, class={change_class})")
    return version_id
