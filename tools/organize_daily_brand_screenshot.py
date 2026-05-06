#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy a screenshot into the daily brand archive structure.")
    parser.add_argument("--source", required=True, help="source image path")
    parser.add_argument("--brand", required=True, help="brand name, e.g. 卫龙")
    parser.add_argument("--city", required=True, help="city alias, e.g. 福州")
    parser.add_argument("--date", required=True, help="date string, e.g. 2026-05-02")
    parser.add_argument(
        "--base-dir",
        default=str(BASE_DIR / "screenshots"),
        help="screenshots base directory",
    )
    args = parser.parse_args()

    source = Path(args.source).resolve()
    if not source.exists():
        raise SystemExit(f"source not found: {source}")

    target_dir = Path(args.base_dir).resolve() / args.brand / args.date
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / f"{args.brand}（{args.city} {args.date}）.png"
    shutil.copy2(source, target_file)
    print(target_file)


if __name__ == "__main__":
    main()
