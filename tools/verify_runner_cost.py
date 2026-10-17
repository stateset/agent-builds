"""Compare queued Buildr package/test work before and after runner changes."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_builds.engine_study import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pairs", type=int, default=4)
    parser.add_argument("--agents", type=int, default=4)
    parser.add_argument("--cpus", type=int, default=2)
    args = parser.parse_args()
    report = run(args.output, args.pairs, args.agents, args.cpus)
    print(
        json.dumps(
            dict(
                result=report["result"],
                output=str(args.output),
                comparisons=report["comparisons"],
                summary=report["summary"],
            ),
            indent=2,
        )
    )
    return 0 if report["verification_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
