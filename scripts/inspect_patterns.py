#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from constella.context_builder.models import SourceRef, Unit
from constella.context_builder.pattern_engine import load_pattern_engine


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect Context Builder pattern configuration.")
    parser.add_argument("command", choices=["list", "test", "validate"])
    parser.add_argument("--group")
    parser.add_argument("--text", default="")
    parser.add_argument("--config", default=str(ROOT / "configs" / "context_builder" / "patterns.yaml"))
    args = parser.parse_args()
    engine = load_pattern_engine(args.config)
    if args.command == "validate":
        print("valid")
    elif args.command == "list":
        groups = [args.group] if args.group else engine.groups.keys()
        for group in groups:
            for rule in engine.groups.get(group, []): print(f"{group}\t{rule['id']}")
    else:
        unit = Unit("test", "passage", args.text, SourceRef())
        for match in (engine.match(args.group, unit) if args.group else engine.match_all(unit)):
            print(match.to_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
