#!/usr/bin/env python3
"""Build a planner_agent plan.json with local Gemma via opencode, verify deterministically.

    python scripts/build_plan.py <plan.json> [--workspace DIR] [--cap N] [--attach URL]

Executes each feasible task in dependency order, verifies its deliverable (parse + stub
check), and writes a build report. No Claude, offline.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from builder import build as build_mod  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", help="path to planner_agent plan.json")
    ap.add_argument("--workspace", default=None, help="build workspace (default: data/builds/<plan>)")
    ap.add_argument("--repo", action="store_true",
                    help="deliver into the project repo's code/ area and commit via reqoach "
                         "(same repo the Analyst/Architect/Planner publish to)")
    ap.add_argument("--pid", default=None, help="project id (default: plan.source.project_id)")
    ap.add_argument("--cap", type=int, default=None, help="max feasible tasks to build")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--attach", default=None, help="persistent opencode serve URL")
    args = ap.parse_args()

    plan = json.load(open(args.plan))
    pid = args.pid or (plan.get("source") or {}).get("project_id")
    plan_name = os.path.splitext(os.path.basename(args.plan))[0]

    if args.repo:
        if not pid:
            sys.exit("--repo needs a project id (pass --pid or use a plan with source.project_id)")
        ws = build_mod.repo_code_workspace(pid)
    else:
        ws = args.workspace or os.path.join(os.path.dirname(__file__), "..", "data", "builds", plan_name)

    report = build_mod.build_plan(plan, ws, cap=args.cap, retries=args.retries, attach=args.attach)

    s = report["summary"]
    print("\n" + "=" * 60)
    print(f"BUILD: {s['built']} built · {s['failed']} failed · {s['no_output']} no-output "
          f"(of {s['total']})")
    print(f"BUILD SUCCESS RATE (built / produced): {s['build_success_rate']}")
    # Report goes to the builder's own data dir (a build artifact, not source code) — the repo's
    # code/ area receives only the built files.
    reportdir = os.path.join(os.path.dirname(__file__), "..", "data", "builds")
    os.makedirs(reportdir, exist_ok=True)
    out = os.path.join(reportdir, plan_name + ".build.json")
    json.dump(report, open(out, "w"), indent=1)
    print(f"-> report: {out}\n-> workspace: {ws}")

    if args.repo:
        commit = build_mod.publish_to_repo(pid)
        print(f"-> committed to repo (code/): {commit.get('sha') or commit}")


if __name__ == "__main__":
    main()
