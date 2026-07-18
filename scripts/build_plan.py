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
from builder.build import build_plan_file  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", help="path to planner_agent plan.json")
    ap.add_argument("--workspace", default=None, help="build workspace (default: data/builds/<plan>)")
    ap.add_argument("--cap", type=int, default=None, help="max feasible tasks to build")
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--attach", default=None, help="persistent opencode serve URL")
    args = ap.parse_args()

    plan_name = os.path.splitext(os.path.basename(args.plan))[0]
    ws = args.workspace or os.path.join(os.path.dirname(__file__), "..", "data", "builds", plan_name)
    report = build_plan_file(args.plan, ws, cap=args.cap, retries=args.retries, attach=args.attach)

    s = report["summary"]
    print("\n" + "=" * 60)
    print(f"BUILD: {s['built']} built · {s['failed']} failed · {s['no_output']} no-output "
          f"(of {s['total']})")
    print(f"BUILD SUCCESS RATE (built / produced): {s['build_success_rate']}")
    out = os.path.join(os.path.dirname(ws), plan_name + ".build.json")
    json.dump(report, open(out, "w"), indent=1)
    print(f"-> report: {out}\n-> workspace: {ws}")


if __name__ == "__main__":
    main()
