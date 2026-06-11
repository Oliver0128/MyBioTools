#!/usr/bin/env python3
import argparse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from token_calendar_core import DEFAULT_DB_PATH, collect_once


def format_ts(timestamp: int | None, timezone: str) -> str:
    if not timestamp:
        return "N/A"
    return datetime.fromtimestamp(timestamp, ZoneInfo(timezone)).isoformat(timespec="seconds")


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect New API token logs into SQLite.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument("--auth-file", default=str(Path.home() / ".codex" / "auth.json"))
    parser.add_argument("--config-file", default=str(Path.home() / ".codex" / "config.toml"))
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--timeout", type=float, default=6.0)
    parser.add_argument("--usage-timeout", type=float, default=2.0)
    parser.add_argument("--timezone", default="Asia/Shanghai")
    args = parser.parse_args()

    result = collect_once(
        db_path=Path(args.db),
        auth_file=Path(args.auth_file),
        config_file=Path(args.config_file),
        page_size=args.page_size,
        timeout=args.timeout,
        usage_timeout=args.usage_timeout,
    )

    print(f"Scanned: {format_ts(result['scanned_at'], args.timezone)}")
    print(f"Source: {result['source_host']}")
    print(f"Fetched: {result['fetched_count']}")
    print(f"Inserted: {result['inserted_count']}")
    print(f"Duplicates: {result['duplicate_count']}")
    print(f"Newest log: {format_ts(result['newest_created_at'], args.timezone)}")
    print(f"Oldest log: {format_ts(result['oldest_created_at'], args.timezone)}")
    if result["gap_risk"]:
        print("WARNING: possible gap. The current fetch window starts after the previous newest stored log.")


if __name__ == "__main__":
    main()
