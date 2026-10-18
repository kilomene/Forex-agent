#!/usr/bin/env python3
"""Version manifests for the evolution pipeline.

versions/<version-id>/version.json carries the full identity of a
candidate version; versions/installed.json tracks which version is live
and which one came before it (rollback target).
"""

import os

from . import lib


def version_path(version_id, bridge=None):
    return os.path.join(lib.version_dir(version_id, bridge), "version.json")


def read_version(version_id, bridge=None):
    return lib.read_json(version_path(version_id, bridge))


def write_version(manifest, bridge=None):
    lib.write_json(version_path(manifest["id"], bridge), manifest)


def new_manifest(version_id, *, parent, change_summary, proposal_id,
                 tier, change_class, files):
    return {
        "id": version_id,
        "utc_timestamp": lib.utc_now_iso(),
        "parent": parent,
        "change_summary": change_summary,
        "proposal_id": proposal_id,
        "tier": tier,                    # "auto" | "zenas"
        "change_class": change_class,    # "refactor" | "behavior"
        "author": lib.AUTHOR,
        "status": "prepared",            # prepared -> tested -> staged ->
                                        # canaried -> installed | rejected
        "stage": None,
        "files": sorted(files),          # changed relpaths
        "gates": {},                     # gate name -> {"ok": bool, ...}
    }


def installed_path(bridge=None):
    return os.path.join(lib.versions_dir(bridge), "installed.json")


def read_installed(bridge=None):
    d = lib.read_json(installed_path(bridge))
    if not d:
        return {"current": None, "previous": None, "updated_utc": None}
    return d


def write_installed(current, previous, bridge=None):
    lib.write_json(installed_path(bridge), {
        "current": current,
        "previous": previous,
        "updated_utc": lib.utc_now_iso(),
    })
