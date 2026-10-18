#!/usr/bin/env python3
"""CLI for the continuous-evolution install pipeline.

  python3 -m pipeline prepare --proposal P-001
  python3 -m pipeline run --version <id> [--soak-signals N]
      [--soak-seconds S] [--skip-supervisor] [--no-restart]
  python3 -m pipeline rollback --previous | --version <id>
  python3 -m pipeline plugin-observe --plugin cooldown_period
      --proposal P-001 [--max-signals N]
  python3 -m pipeline plugin-enforce --plugin cooldown_period
      --proposal P-001
  python3 -m pipeline status [--version <id>]

Global: --bridge-root DIR --state-dir DIR
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from pipeline import lib, manifest, prepare, canary  # noqa: E402
from pipeline import run_pipeline  # noqa: E402


def _common(ap):
    ap.add_argument("--bridge-root", default=None)
    ap.add_argument("--state-dir", default=None)


def main(argv):
    ap = argparse.ArgumentParser(prog="pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare")
    p.add_argument("--proposal", required=True)
    p.add_argument("--summary", default=None)
    _common(p)

    p = sub.add_parser("run")
    p.add_argument("--version", required=True)
    p.add_argument("--soak-signals", type=int,
                   default=canary.DEFAULT_SOAK_SIGNALS)
    p.add_argument("--soak-seconds", type=int,
                   default=canary.DEFAULT_SOAK_SECONDS)
    p.add_argument("--skip-supervisor", action="store_true")
    p.add_argument("--no-restart", action="store_true")
    p.add_argument("--journal-path", default=None)
    _common(p)

    p = sub.add_parser("rollback")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--previous", action="store_true")
    g.add_argument("--version", default=None)
    p.add_argument("--no-restart", action="store_true")
    _common(p)

    p = sub.add_parser("plugin-observe")
    p.add_argument("--plugin", required=True)
    p.add_argument("--proposal", required=True)
    p.add_argument("--max-signals", type=int, default=500)
    p.add_argument("--journal-path", default=None)
    _common(p)

    p = sub.add_parser("plugin-enforce")
    p.add_argument("--plugin", required=True)
    p.add_argument("--proposal", required=True)
    _common(p)

    p = sub.add_parser("status")
    p.add_argument("--version", default=None)
    _common(p)

    args = ap.parse_args(argv)
    bridge = lib.bridge_root(args.bridge_root)
    if args.state_dir:
        os.environ["TRADER_STATE_DIR"] = os.path.abspath(args.state_dir)

    if args.cmd == "prepare":
        vid = prepare.prepare_from_proposal(
            args.proposal, bridge, summary_override=args.summary)
        print(f"prepared {vid}")
        return 0

    if args.cmd == "run":
        try:
            ok, man = run_pipeline(
                args.version, bridge,
                state_override=(os.path.abspath(args.state_dir)
                                if args.state_dir else None),
                soak_signals=args.soak_signals,
                soak_seconds=args.soak_seconds,
                skip_supervisor=args.skip_supervisor,
                no_restart=args.no_restart,
                journal_path=args.journal_path)
        except lib.PipelineError as e:
            print(f"REJECTED: {e}", flush=True)
            return 4
        except lib.PipelineHalt as e:
            print(f"HALTED: {e}", flush=True)
            return 5
        print(f"version {args.version}: "
              f"{'INSTALLED' if ok else 'REJECTED'}")
        return 0 if ok else 4

    if args.cmd == "rollback":
        rargs = ["--previous" if args.previous
                 else "--version", args.version or ""]
        if args.previous:
            rargs = ["--previous"]
        else:
            rargs = ["--version", args.version]
        rargs += ["--bridge-root", bridge]
        if args.state_dir:
            rargs += ["--state-dir", os.path.abspath(args.state_dir)]
        if args.no_restart:
            rargs.append("--no-restart")
        import subprocess
        r = subprocess.run(
            [sys.executable, os.path.join(bridge, "rollback.py")] + rargs,
            capture_output=True, text=True)
        print(r.stdout, end="")
        print(r.stderr, end="", file=sys.stderr)
        return r.returncode

    if args.cmd == "plugin-observe":
        try:
            ok, rep = canary.observe_plugin(
                args.plugin, args.proposal, bridge,
                state_override=(os.path.abspath(args.state_dir)
                                if args.state_dir else None),
                max_signals=args.max_signals,
                journal_path=args.journal_path)
        except (lib.PipelineError, lib.PipelineHalt) as e:
            print(f"OBSERVE FAILED: {e}", flush=True)
            return 4
        print(json.dumps(rep, indent=2))
        return 0 if ok else 4

    if args.cmd == "plugin-enforce":
        try:
            rep = canary.enforce_plugin(
                args.plugin, args.proposal, bridge,
                state_override=(os.path.abspath(args.state_dir)
                                if args.state_dir else None))
        except (lib.PipelineError, lib.PipelineHalt) as e:
            print(f"ENFORCE REFUSED: {e}", flush=True)
            return 4
        print(json.dumps(rep, indent=2))
        return 0

    if args.cmd == "status":
        inst = manifest.read_installed(bridge)
        print(json.dumps(inst, indent=2))
        if args.version:
            man = manifest.read_version(args.version, bridge)
            print(json.dumps(man, indent=2) if man else "unknown version")
        else:
            vd = lib.versions_dir(bridge)
            if os.path.isdir(vd):
                for v in sorted(os.listdir(vd)):
                    m = manifest.read_version(v, bridge)
                    if m:
                        print(f"{v}: {m.get('status')} "
                              f"({m.get('change_summary','')[:60]})")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
