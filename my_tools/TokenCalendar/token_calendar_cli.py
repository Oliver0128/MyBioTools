#!/usr/bin/env python3
import argparse
import curses
import math
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from token_calendar_core import (
    DEFAULT_DB_PATH,
    QUOTA_PER_USD,
    as_int,
    collect_once,
    db_stats,
    load_latest_usage,
    load_logs_from_db,
)
from token_calendar_fx import DEFAULT_FX_CACHE_PATH, FxRate, load_usd_cny_rate
from token_calendar_pricing import DEFAULT_PRICING_CACHE_PATH, PricingCatalog


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
METRICS = {
    "tokens": "total_tokens",
    "input": "input_tokens",
    "output": "output_tokens",
    "cache-read": "cache_read_tokens",
    "cache-create": "cache_create_tokens",
    "cache-hit": "cache_hit_rate",
    "quota": "quota",
    "requests": "requests",
    "usd": "estimated_usd",
    "official-usd": "official_usd",
}
METRIC_LABELS = {
    "tokens": "Total tokens",
    "input": "Input tokens",
    "output": "Output tokens",
    "cache-read": "Cache read",
    "cache-create": "Cache create",
    "cache-hit": "Cache hit rate",
    "quota": "Quota",
    "requests": "Requests",
    "usd": "NewAPI CNY",
    "official-usd": "Official CNY",
}
METRIC_HOTKEYS = {
    "1": "tokens",
    "2": "input",
    "3": "output",
    "4": "cache-read",
    "5": "cache-create",
    "6": "requests",
    "7": "quota",
    "8": "usd",
    "9": "cache-hit",
    "0": "official-usd",
}

CACHE_READ_KEYS = (
    "cache_tokens",
    "cached_tokens",
    "cached_input_tokens",
    "input_cached_tokens",
    "cache_read_tokens",
    "cache_read_input_tokens",
)
CACHE_CREATE_KEYS = (
    "cache_creation_input_tokens",
    "cache_create_tokens",
    "cache_creation_tokens",
    "cache_write_tokens",
    "cache_write_input_tokens",
)

MAX_MODEL_TABLE_WIDTH = 126
MAX_IP_TABLE_WIDTH = 104
MAX_DETAIL_PANEL_WIDTH = 78
MAX_CALENDAR_PANEL_WIDTH = 168


def human(value: float) -> str:
    sign = "-" if value < 0 else ""
    value = abs(float(value))
    for suffix, size in (("T", 1_000_000_000_000), ("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if value >= size:
            return f"{sign}{value / size:.1f}{suffix}"
    if value == int(value):
        return f"{sign}{int(value):,}"
    return f"{sign}{value:,.2f}"


def yuan(value: float) -> str:
    return f"¥{value:,.4f}"


def newapi_money(value: float) -> str:
    return yuan(value)


def official_money(value: float, fx: FxRate | None) -> str:
    if not fx or fx.usd_cny <= 0:
        return "¥N/A"
    return yuan(value * fx.usd_cny)


def fx_text(fx: FxRate | None) -> str:
    if not fx:
        return "FX unavailable"
    if fx.usd_cny <= 0:
        return f"FX USD/CNY unavailable for {fx.date}"
    return f"FX USD/CNY {fx.usd_cny:.4f} for {fx.date}"


def load_fx(args: argparse.Namespace) -> FxRate:
    return load_usd_cny_rate(
        timezone=args.timezone,
        cache_path=Path(args.fx_cache),
        timeout=args.fx_timeout,
        offline=args.offline_fx,
    )


def pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.1f}%"


def mask_ip(ip: str, show_full: bool = False) -> str:
    ip = str(ip or "").strip()
    if not ip:
        return "unknown"
    if show_full:
        return ip
    if "." in ip:
        parts = ip.split(".")
        if len(parts) == 4:
            return ".".join(parts[:2] + ["xxx", "xxx"])
    if ":" in ip:
        parts = ip.split(":")
        return ":".join(parts[:3] + ["xxxx"]) if len(parts) > 3 else ip
    return ip[:6] + "..." if len(ip) > 8 else ip


def cache_hit_rate(input_tokens: int, cache_create: int, cache_read: int) -> float | None:
    denominator = input_tokens + cache_create + cache_read
    if denominator <= 0:
        return None
    return cache_read / denominator


def format_ts(timestamp: int | None, timezone: str) -> str:
    if not timestamp:
        return "N/A"
    return datetime.fromtimestamp(timestamp, ZoneInfo(timezone)).strftime("%Y-%m-%d %H:%M:%S")


def clock_time(timestamp: float | None, timezone: str) -> str:
    if not timestamp:
        return "N/A"
    return datetime.fromtimestamp(timestamp, ZoneInfo(timezone)).strftime("%H:%M:%S")


def interval_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds}s"


def parse_other(row: dict) -> dict:
    other = row.get("other")
    if not other:
        return {}
    if isinstance(other, dict):
        return other
    try:
        import json

        return json.loads(other)
    except Exception:
        return {}


def first_int(payload: dict, keys: tuple[str, ...]) -> int:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return as_int(value)
    return 0


def token_parts(row: dict) -> dict:
    other = parse_other(row)
    output = as_int(row.get("completion_tokens"))
    prompt_total = as_int(row.get("prompt_tokens"))
    cache_read = first_int(other, CACHE_READ_KEYS)
    cache_create = first_int(other, CACHE_CREATE_KEYS)
    input_tokens = max(0, prompt_total - cache_read - cache_create)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output,
        "cache_read_tokens": cache_read,
        "cache_create_tokens": cache_create,
        "total_tokens": input_tokens + output + cache_read + cache_create,
        "prompt_total": prompt_total,
    }


def window_start_timestamp(days: int, timezone: str) -> int:
    tz = ZoneInfo(timezone)
    today = datetime.now(tz).date()
    range_start = today - timedelta(days=days - 1)
    align_start = range_start - timedelta(days=range_start.weekday())
    return int(datetime.combine(align_start, datetime.min.time(), tzinfo=tz).timestamp())


def aggregate(rows: list[dict], days: int, timezone: str, pricing: PricingCatalog) -> dict:
    tz = ZoneInfo(timezone)
    today = datetime.now(tz).date()
    range_start = today - timedelta(days=days - 1)
    align_start = range_start - timedelta(days=range_start.weekday())
    align_end = today + timedelta(days=6 - today.weekday())

    daily = defaultdict(Counter)
    models = defaultdict(Counter)
    totals = Counter()
    first_day = None
    last_day = None
    token_name = ""

    for row in rows:
        created_at = as_int(row.get("created_at"))
        if not created_at:
            continue
        day = datetime.fromtimestamp(created_at, tz).date()
        parts = token_parts(row)
        prompt = parts["input_tokens"]
        completion = parts["output_tokens"]
        cache_read = parts["cache_read_tokens"]
        cache_create = parts["cache_create_tokens"]
        tokens = parts["total_tokens"]
        quota = as_int(row.get("quota"))
        model = str(row.get("model_name") or "unknown")
        official_cost = pricing.official_cost_usd(model, prompt, completion, cache_read, cache_create)

        if not token_name and row.get("token_name"):
            token_name = str(row["token_name"])

        bucket = daily[day.isoformat()]
        bucket["requests"] += 1
        bucket["input_tokens"] += prompt
        bucket["output_tokens"] += completion
        bucket["cache_read_tokens"] += cache_read
        bucket["cache_create_tokens"] += cache_create
        bucket["total_tokens"] += tokens
        bucket["quota"] += quota
        bucket["official_usd"] += official_cost
        bucket["estimated_usd"] = bucket["quota"] / QUOTA_PER_USD

        totals["requests"] += 1
        totals["input_tokens"] += prompt
        totals["output_tokens"] += completion
        totals["cache_read_tokens"] += cache_read
        totals["cache_create_tokens"] += cache_create
        totals["total_tokens"] += tokens
        totals["quota"] += quota
        totals["official_usd"] += official_cost

        models[model]["requests"] += 1
        models[model]["input_tokens"] += prompt
        models[model]["output_tokens"] += completion
        models[model]["cache_read_tokens"] += cache_read
        models[model]["cache_create_tokens"] += cache_create
        models[model]["total_tokens"] += tokens
        models[model]["quota"] += quota
        models[model]["official_usd"] += official_cost

        first_day = day if first_day is None else min(first_day, day)
        last_day = day if last_day is None else max(last_day, day)

    cells = []
    current = align_start
    index = 0
    while current <= align_end:
        key = current.isoformat()
        bucket = daily[key]
        cells.append(
            {
                "date": key,
                "week": index // 7,
                "weekday": index % 7,
                "in_range": range_start <= current <= today,
                "requests": bucket["requests"],
                "input_tokens": bucket["input_tokens"],
                "output_tokens": bucket["output_tokens"],
                "cache_read_tokens": bucket["cache_read_tokens"],
                "cache_create_tokens": bucket["cache_create_tokens"],
                "total_tokens": bucket["total_tokens"],
                "cache_hit_rate": cache_hit_rate(
                    bucket["input_tokens"],
                    bucket["cache_create_tokens"],
                    bucket["cache_read_tokens"],
                ),
                "quota": bucket["quota"],
                "estimated_usd": bucket["quota"] / QUOTA_PER_USD,
                "official_usd": bucket["official_usd"],
            }
        )
        current += timedelta(days=1)
        index += 1

    totals["estimated_usd"] = totals["quota"] / QUOTA_PER_USD
    totals["cache_hit_rate"] = cache_hit_rate(
        totals["input_tokens"],
        totals["cache_create_tokens"],
        totals["cache_read_tokens"],
    )
    top_models = sorted(
        (
            {
                "model": model,
                "requests": counter["requests"],
                "input_tokens": counter["input_tokens"],
                "output_tokens": counter["output_tokens"],
                "cache_read_tokens": counter["cache_read_tokens"],
                "cache_create_tokens": counter["cache_create_tokens"],
                "total_tokens": counter["total_tokens"],
                "cache_hit_rate": cache_hit_rate(
                    counter["input_tokens"],
                    counter["cache_create_tokens"],
                    counter["cache_read_tokens"],
                ),
                "quota": counter["quota"],
                "estimated_usd": counter["quota"] / QUOTA_PER_USD,
                "official_usd": counter["official_usd"],
            }
            for model, counter in models.items()
        ),
        key=lambda item: (item["total_tokens"], item["quota"]),
        reverse=True,
    )

    return {
        "today": today,
        "range_start": range_start,
        "range_end": today,
        "first_day": first_day,
        "last_day": last_day,
        "weeks": math.ceil(len(cells) / 7),
        "cells": cells,
        "totals": dict(totals),
        "top_models": top_models,
        "token_name": token_name,
    }


def metric_value(cell: dict, metric_key: str) -> float:
    value = cell.get(metric_key, 0)
    if value is None:
        return 0.0
    return float(value)


def metric_max(cells: list[dict], metric_key: str) -> float:
    return max((metric_value(cell, metric_key) for cell in cells if cell["in_range"]), default=0.0)


def level(value: float, max_value: float) -> int:
    if value <= 0 or max_value <= 0:
        return 0
    ratio = math.sqrt(value / max_value)
    if ratio <= 0.25:
        return 1
    if ratio <= 0.5:
        return 2
    if ratio <= 0.75:
        return 3
    return 4


def color_enabled(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never" or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def paint_cell(cell_level: int, use_color: bool, compact: bool) -> str:
    if not use_color:
        chars = [" ", ".", ":", "+", "#"]
        text = chars[cell_level]
        return text if compact else text * 2

    bg = [250, 152, 79, 36, 22][cell_level]
    width = 1 if compact else 2
    return f"\033[48;5;{bg}m{' ' * width}\033[0m"


def paint_text(text: str, color_code: str, use_color: bool) -> str:
    if not use_color:
        return text
    return f"\033[{color_code}m{text}\033[0m"


def fit(text: str, width: int) -> str:
    text = str(text)
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def right(value: str, width: int) -> str:
    return fit(value, width).rjust(width)


def model_table_width(width: int) -> int:
    return max(40, min(width, MAX_MODEL_TABLE_WIDTH))


def model_table_header(width: int) -> str:
    width = model_table_width(width)
    if width >= 104:
        tail = (
            f" {right('Input', 7)} {right('Output', 8)} {right('CacheC', 7)} "
            f"{right('CacheR', 7)} {right('Hit%', 6)} {right('Total', 8)} "
            f"{right('NewAPI', 10)} {right('Official', 12)} {right('Req', 5)}"
        )
    elif width >= 76:
        tail = (
            f" {right('Input', 7)} {right('Output', 7)} {right('CacheR', 7)} {right('Hit%', 6)} "
            f"{right('Total', 8)} {right('Official', 12)} {right('Req', 5)}"
        )
    else:
        tail = f" {right('Official', 12)} {right('Total', 8)} {right('Req', 5)}"
    name_width = max(8, width - len(tail))
    return fit(f"{'Model'.ljust(name_width)}{tail}", width)


def model_table_row(item: dict, width: int, fx: FxRate | None) -> str:
    width = model_table_width(width)
    if width >= 104:
        tail = (
            f" {right(human(item.get('input_tokens', 0)), 7)}"
            f" {right(human(item.get('output_tokens', 0)), 8)}"
            f" {right(human(item.get('cache_create_tokens', 0)), 7)}"
            f" {right(human(item.get('cache_read_tokens', 0)), 7)}"
            f" {right(pct(item.get('cache_hit_rate')), 6)}"
            f" {right(human(item.get('total_tokens', 0)), 8)}"
            f" {right(newapi_money(item.get('estimated_usd', 0)), 10)}"
            f" {right(official_money(item.get('official_usd', 0), fx), 12)}"
            f" {right(str(item.get('requests', 0)), 5)}"
        )
    elif width >= 76:
        tail = (
            f" {right(human(item.get('input_tokens', 0)), 7)}"
            f" {right(human(item.get('output_tokens', 0)), 7)}"
            f" {right(human(item.get('cache_read_tokens', 0)), 7)}"
            f" {right(pct(item.get('cache_hit_rate')), 6)}"
            f" {right(human(item.get('total_tokens', 0)), 8)}"
            f" {right(official_money(item.get('official_usd', 0), fx), 12)}"
            f" {right(str(item.get('requests', 0)), 5)}"
        )
    else:
        tail = (
            f" {right(official_money(item.get('official_usd', 0), fx), 12)}"
            f" {right(human(item.get('total_tokens', 0)), 8)}"
            f" {right(str(item.get('requests', 0)), 5)}"
        )
    name_width = max(8, width - len(tail))
    return fit(f"{fit(item.get('model', 'unknown'), name_width).ljust(name_width)}{tail}", width)


def render_heatmap(data: dict, metric_key: str, use_color: bool, compact: bool) -> list[str]:
    weeks = data["weeks"]
    cells = data["cells"]
    max_value = metric_max(cells, metric_key)
    slot_width = 1 if compact else 3
    label_width = 4

    month_slots = [" " * slot_width for _ in range(weeks)]
    seen = set()
    for cell in cells:
        if not cell["in_range"]:
            continue
        month_key = cell["date"][:7]
        if month_key in seen:
            continue
        seen.add(month_key)
        month = MONTHS[int(cell["date"][5:7]) - 1]
        label = month[:1] if compact else month[:3]
        month_slots[cell["week"]] = label.ljust(slot_width)[:slot_width]

    by_pos = {(cell["week"], cell["weekday"]): cell for cell in cells}
    lines = [" " * label_width + "".join(month_slots).rstrip()]
    for weekday_index, weekday in enumerate(WEEKDAYS):
        row = [weekday.ljust(label_width)]
        for week in range(weeks):
            cell = by_pos.get((week, weekday_index))
            if not cell or not cell["in_range"]:
                row.append(" " if compact else "  ")
            else:
                row.append(paint_cell(level(metric_value(cell, metric_key), max_value), use_color, compact))
            if not compact:
                row.append(" ")
        lines.append("".join(row).rstrip())

    less = paint_cell(0, use_color, compact)
    more = paint_cell(4, use_color, compact)
    lines.append("")
    lines.append(f"Less {less} {paint_cell(1, use_color, compact)} {paint_cell(2, use_color, compact)} {paint_cell(3, use_color, compact)} {more} More")
    return lines


def render_models(models: list[dict], limit: int, width: int = 96, fx: FxRate | None = None) -> list[str]:
    if not models:
        return ["Top models: none"]
    shown = models[:limit]
    table_width = model_table_width(width)
    lines = ["Top models", "  " + model_table_header(table_width)]
    for item in shown:
        lines.append("  " + model_table_row(item, table_width, fx))
    return lines


def render_ips(ips: list[dict], limit: int, width: int = 96, show_full: bool = False, fx: FxRate | None = None) -> list[str]:
    if not ips:
        return ["IP distribution: none"]
    table_width = max(44, min(width, MAX_IP_TABLE_WIDTH))
    lines = ["IP distribution", "  " + ip_table_header(table_width)]
    for item in ips[:limit]:
        lines.append("  " + ip_table_row(item, table_width, show_full, fx))
    return lines


def wrap_text(text: str, width: int, indent: str = "") -> list[str]:
    import textwrap

    width = max(10, width)
    wrapped = textwrap.wrap(
        str(text),
        width=width,
        subsequent_indent=indent,
        break_long_words=True,
        break_on_hyphens=False,
    )
    return wrapped or [""]


def append_wrapped(lines: list[str], text: str, width: int, indent: str = "") -> None:
    lines.extend(wrap_text(text, width, indent=indent))


def append_key_values(lines: list[str], items: list[tuple[str, str]], width: int) -> None:
    for label, value in items:
        append_wrapped(lines, f"{label}: {value}", width, indent="  ")


def text_heatmap_lines(data: dict, metric_key: str, selected_date: str, width: int) -> list[str]:
    weeks = data["weeks"]
    cells = data["cells"]
    max_value = metric_max(cells, metric_key)
    chars = ["·", "░", "▒", "▓", "█"]
    label_width = 4
    available = max(1, width - label_width)
    visible_weeks = max(1, min(weeks, available // 2 if available >= 18 else available))
    selected_week = next((cell["week"] for cell in cells if cell["date"] == selected_date), weeks - 1)
    first_week = min(max(0, selected_week - visible_weeks + 1), max(0, weeks - visible_weeks))
    last_week = first_week + visible_weeks - 1
    gap = "" if visible_weeks > 18 else " "

    month_slots = [" " for _ in range(visible_weeks)]
    seen_months = set()
    for cell in cells:
        if not cell["in_range"] or not first_week <= cell["week"] <= last_week:
            continue
        month_key = cell["date"][:7]
        if month_key in seen_months:
            continue
        seen_months.add(month_key)
        month_slots[cell["week"] - first_week] = MONTHS[int(cell["date"][5:7]) - 1][:1]

    by_pos = {(cell["week"], cell["weekday"]): cell for cell in cells}
    lines = [" " * label_width + gap.join(month_slots).rstrip()]
    for weekday_index, weekday in enumerate(WEEKDAYS):
        row = [weekday.ljust(label_width)]
        for week in range(first_week, last_week + 1):
            cell = by_pos.get((week, weekday_index))
            if not cell or not cell["in_range"]:
                row.append(" ")
            elif cell["date"] == selected_date:
                row.append("@")
            else:
                row.append(chars[level(metric_value(cell, metric_key), max_value)])
            if gap:
                row.append(gap)
        lines.append("".join(row).rstrip())
    if first_week > 0 or last_week < weeks - 1:
        lines.append(f"weeks {first_week + 1}-{last_week + 1}/{weeks}; move day to pan")
    lines.append("Less · ░ ▒ ▓ █ More   @ selected")
    return lines


def stacked_model_lines(models: list[dict], limit: int, width: int, fx: FxRate | None) -> list[str]:
    lines = ["Top models"]
    for item in models[:limit]:
        append_wrapped(lines, f"Model: {item.get('model', 'unknown')}", width, indent="  ")
        append_wrapped(
            lines,
            "  "
            f"Req {item.get('requests', 0)} | "
            f"Tokens {human(item.get('total_tokens', 0))} | "
            f"Input {human(item.get('input_tokens', 0))} | "
            f"Output {human(item.get('output_tokens', 0))} | "
            f"CacheR {human(item.get('cache_read_tokens', 0))} | "
            f"CacheC {human(item.get('cache_create_tokens', 0))} | "
            f"Hit {pct(item.get('cache_hit_rate'))} | "
            f"NewAPI {newapi_money(item.get('estimated_usd', 0))} | "
            f"Official {official_money(item.get('official_usd', 0), fx)}",
            width,
            indent="  ",
        )
    return lines


def stacked_ip_lines(ips: list[dict], limit: int, width: int, show_full: bool, fx: FxRate | None) -> list[str]:
    lines = ["IP distribution"]
    for item in ips[:limit]:
        ip_text = mask_ip(item.get("ip", ""), show_full)
        append_wrapped(
            lines,
            f"{ip_text}: Req {item.get('requests', 0)} | "
            f"Tokens {human(item.get('total_tokens', 0))} | "
            f"NewAPI {newapi_money(item.get('estimated_usd', 0))} | "
            f"Official {official_money(item.get('official_usd', 0), fx)}",
            width,
            indent="  ",
        )
    return lines


def load_dashboard_context(db_path: Path, args: argparse.Namespace, pricing: PricingCatalog, fx: FxRate) -> dict:
    start_ts = window_start_timestamp(args.days, args.timezone)
    rows = load_logs_from_db(db_path, start_timestamp=start_ts)
    usage, source_host = load_latest_usage(db_path)
    stats = db_stats(db_path)
    data = aggregate(rows, args.days, args.timezone, pricing)
    return {
        "rows": rows,
        "usage": usage,
        "source_host": source_host,
        "stats": stats,
        "data": data,
        "pricing": pricing,
        "fx": fx,
    }


def safe_addstr(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    if x < 0:
        text = text[-x:]
        x = 0
    if not text:
        return
    try:
        stdscr.addstr(y, x, text[: max(0, width - x - 1)], attr)
    except curses.error:
        pass


def init_colors() -> dict[str, int]:
    curses.start_color()
    curses.use_default_colors()
    result = {"_256": curses.COLORS >= 256}
    if curses.COLORS >= 256:
        pairs = {
            "muted": (1, 244, -1),
            "title": (2, 81, -1),
            "accent": (3, 37, -1),
            "warn": (4, 214, -1),
            "danger": (5, 203, -1),
            "line": (6, 240, -1),
            "cell0": (10, 250, 250),
            "cell1": (11, 16, 116),
            "cell2": (12, 16, 73),
            "cell3": (13, 16, 37),
            "cell4": (14, 16, 23),
            "selected": (15, 16, 110),
        }
    else:
        pairs = {
            "muted": (1, curses.COLOR_WHITE, -1),
            "title": (2, curses.COLOR_CYAN, -1),
            "accent": (3, curses.COLOR_GREEN, -1),
            "warn": (4, curses.COLOR_YELLOW, -1),
            "danger": (5, curses.COLOR_RED, -1),
            "line": (6, curses.COLOR_BLUE, -1),
            "cell0": (10, curses.COLOR_WHITE, -1),
            "cell1": (11, curses.COLOR_GREEN, -1),
            "cell2": (12, curses.COLOR_CYAN, -1),
            "cell3": (13, curses.COLOR_BLUE, -1),
            "cell4": (14, curses.COLOR_MAGENTA, -1),
            "selected": (15, curses.COLOR_BLACK, curses.COLOR_WHITE),
        }
    for name, (idx, fg, bg) in pairs.items():
        try:
            curses.init_pair(idx, fg, bg)
            result[name] = curses.color_pair(idx)
        except (curses.error, ValueError):
            result[name] = 0
    return result


def cell_attr(cell_level: int, selected: bool, colors: dict[str, int]) -> int:
    if selected:
        return colors.get("selected", 0) | curses.A_BOLD | curses.A_REVERSE
    return colors.get(f"cell{cell_level}", 0)


def tui_cell_text(cell_level: int, cell_width: int, colors: dict[str, int], force_text: bool = False) -> str:
    if colors.get("_256") and not force_text:
        return " " * cell_width
    chars = ["·", "░", "▒", "▓", "█"]
    text = chars[max(0, min(cell_level, len(chars) - 1))]
    return text if cell_width <= 1 else text * cell_width


def selected_cell(cells: list[dict]) -> int:
    for index in range(len(cells) - 1, -1, -1):
        if cells[index]["in_range"]:
            return index
    return 0


def move_selection(data: dict, selected: int, delta_week: int = 0, delta_day: int = 0) -> int:
    cells = data["cells"]
    current = cells[selected]
    target_week = current["week"] + delta_week
    target_weekday = current["weekday"] + delta_day
    while target_weekday < 0:
        target_weekday += 7
        target_week -= 1
    while target_weekday > 6:
        target_weekday -= 7
        target_week += 1

    candidates = [
        (idx, cell)
        for idx, cell in enumerate(cells)
        if cell["week"] == target_week and cell["weekday"] == target_weekday and cell["in_range"]
    ]
    if candidates:
        return candidates[0][0]
    return selected


def model_breakdown(rows: list[dict], date_key: str, timezone: str, pricing: PricingCatalog) -> list[dict]:
    tz = ZoneInfo(timezone)
    models = defaultdict(Counter)
    for row in rows:
        created_at = as_int(row.get("created_at"))
        if not created_at:
            continue
        if datetime.fromtimestamp(created_at, tz).date().isoformat() != date_key:
            continue
        model = str(row.get("model_name") or "unknown")
        parts = token_parts(row)
        quota = as_int(row.get("quota"))
        official_cost = pricing.official_cost_usd(
            model,
            parts["input_tokens"],
            parts["output_tokens"],
            parts["cache_read_tokens"],
            parts["cache_create_tokens"],
        )
        models[model]["requests"] += 1
        models[model]["input_tokens"] += parts["input_tokens"]
        models[model]["output_tokens"] += parts["output_tokens"]
        models[model]["cache_read_tokens"] += parts["cache_read_tokens"]
        models[model]["cache_create_tokens"] += parts["cache_create_tokens"]
        models[model]["total_tokens"] += parts["total_tokens"]
        models[model]["quota"] += quota
        models[model]["official_usd"] += official_cost
    return sorted(
        (
            {
                "model": model,
                "requests": counter["requests"],
                "input_tokens": counter["input_tokens"],
                "output_tokens": counter["output_tokens"],
                "cache_read_tokens": counter["cache_read_tokens"],
                "cache_create_tokens": counter["cache_create_tokens"],
                "total_tokens": counter["total_tokens"],
                "cache_hit_rate": cache_hit_rate(
                    counter["input_tokens"],
                    counter["cache_create_tokens"],
                    counter["cache_read_tokens"],
                ),
                "quota": counter["quota"],
                "estimated_usd": counter["quota"] / QUOTA_PER_USD,
                "official_usd": counter["official_usd"],
            }
            for model, counter in models.items()
        ),
        key=lambda item: (item["total_tokens"], item["quota"]),
        reverse=True,
    )


def ip_breakdown(rows: list[dict], date_key: str | None, timezone: str, pricing: PricingCatalog) -> list[dict]:
    tz = ZoneInfo(timezone)
    ips = defaultdict(Counter)
    for row in rows:
        created_at = as_int(row.get("created_at"))
        if not created_at:
            continue
        if date_key and datetime.fromtimestamp(created_at, tz).date().isoformat() != date_key:
            continue
        ip = str(row.get("ip") or "").strip() or "unknown"
        model = str(row.get("model_name") or "unknown")
        parts = token_parts(row)
        quota = as_int(row.get("quota"))
        official_cost = pricing.official_cost_usd(
            model,
            parts["input_tokens"],
            parts["output_tokens"],
            parts["cache_read_tokens"],
            parts["cache_create_tokens"],
        )
        ips[ip]["requests"] += 1
        ips[ip]["input_tokens"] += parts["input_tokens"]
        ips[ip]["output_tokens"] += parts["output_tokens"]
        ips[ip]["cache_read_tokens"] += parts["cache_read_tokens"]
        ips[ip]["cache_create_tokens"] += parts["cache_create_tokens"]
        ips[ip]["total_tokens"] += parts["total_tokens"]
        ips[ip]["quota"] += quota
        ips[ip]["official_usd"] += official_cost
    return sorted(
        (
            {
                "ip": ip,
                "requests": counter["requests"],
                "input_tokens": counter["input_tokens"],
                "output_tokens": counter["output_tokens"],
                "cache_read_tokens": counter["cache_read_tokens"],
                "cache_create_tokens": counter["cache_create_tokens"],
                "total_tokens": counter["total_tokens"],
                "quota": counter["quota"],
                "estimated_usd": counter["quota"] / QUOTA_PER_USD,
                "official_usd": counter["official_usd"],
            }
            for ip, counter in ips.items()
        ),
        key=lambda item: (item["requests"], item["total_tokens"], item["quota"]),
        reverse=True,
    )


def draw_box(stdscr, y: int, x: int, h: int, w: int, title: str, colors: dict[str, int]) -> None:
    if h < 3 or w < 8:
        return
    line_attr = colors.get("line", 0)
    safe_addstr(stdscr, y, x, "┌" + "─" * (w - 2) + "┐", line_attr)
    for row in range(1, h - 1):
        safe_addstr(stdscr, y + row, x, "│", line_attr)
        safe_addstr(stdscr, y + row, x + w - 1, "│", line_attr)
    safe_addstr(stdscr, y + h - 1, x, "└" + "─" * (w - 2) + "┘", line_attr)
    if title:
        safe_addstr(stdscr, y, x + 2, f" {title} ", colors.get("title", 0) | curses.A_BOLD)


def draw_key_values(stdscr, y: int, x: int, width: int, items: list[tuple[str, str]], colors: dict[str, int]) -> int:
    row = y
    col = x
    for label, value in items:
        text = f"{label} {value}"
        if col > x and col + len(text) + 2 >= x + width:
            row += 1
            col = x
        if row >= curses.LINES - 1:
            break
        safe_addstr(stdscr, row, col, f"{label} ", colors.get("muted", 0))
        safe_addstr(stdscr, row, col + len(label) + 1, value, curses.A_BOLD)
        col += len(text) + 3
    return row + 1


def ip_table_header(width: int) -> str:
    table_width = max(44, min(width, MAX_IP_TABLE_WIDTH))
    if table_width >= 76:
        tail = (
            f" {right('Req', 5)} {right('Tokens', 8)} "
            f"{right('NewAPI', 10)} {right('Official', 12)}"
        )
    else:
        tail = f" {right('Req', 5)} {right('NewAPI', 10)} {right('Official', 12)}"
    name_width = max(8, table_width - len(tail))
    return fit(f"{'IP'.ljust(name_width)}{tail}", table_width)


def ip_table_row(item: dict, width: int, show_full: bool, fx: FxRate | None) -> str:
    table_width = max(44, min(width, MAX_IP_TABLE_WIDTH))
    if table_width >= 76:
        tail = (
            f" {right(str(item.get('requests', 0)), 5)}"
            f" {right(human(item.get('total_tokens', 0)), 8)}"
            f" {right(newapi_money(item.get('estimated_usd', 0)), 10)}"
            f" {right(official_money(item.get('official_usd', 0), fx), 12)}"
        )
    else:
        tail = (
            f" {right(str(item.get('requests', 0)), 5)}"
            f" {right(newapi_money(item.get('estimated_usd', 0)), 10)}"
            f" {right(official_money(item.get('official_usd', 0), fx), 12)}"
        )
    name_width = max(8, table_width - len(tail))
    ip_text = mask_ip(item.get("ip", ""), show_full)
    return fit(f"{fit(ip_text, name_width).ljust(name_width)}{tail}", table_width)


def draw_tui(stdscr, context: dict, args: argparse.Namespace) -> None:
    data = context["data"]
    rows = context["rows"]
    usage = context["usage"]
    source_host = context["source_host"]
    stats = context["stats"]
    pricing = context["pricing"]
    fx = context["fx"]
    db_path = context.get("db_path")
    metric_key = METRICS[args.metric]
    colors = init_colors()

    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(200)
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
        print("\033[?1003h", end="", flush=True)
    except curses.error:
        pass

    selected = selected_cell(data["cells"])
    status_message = context.get("status_message", "")
    watch_interval = max(1.0, float(args.watch_interval))
    live_enabled = args.watch_interval > 0 and not args.no_watch
    last_live_sync = time.time() if not args.no_refresh else None
    next_live_sync = time.time() + watch_interval if live_enabled else None
    syncing = False
    sync_future = None
    executor = ThreadPoolExecutor(max_workers=1) if live_enabled else None
    scroll_offset = 0
    cell_positions: dict[tuple[int, int], int] = {}
    day_model_cache: dict[str, list[dict]] = {}
    day_ip_cache: dict[str, list[dict]] = {}

    def reload_context(new_context: dict) -> None:
        nonlocal data, rows, usage, source_host, stats, pricing, fx, selected, day_model_cache, day_ip_cache
        old_date = data["cells"][selected]["date"] if data.get("cells") else ""
        data = new_context["data"]
        rows = new_context["rows"]
        usage = new_context["usage"]
        source_host = new_context["source_host"]
        stats = new_context["stats"]
        pricing = new_context["pricing"]
        fx = new_context["fx"]
        day_model_cache = {}
        day_ip_cache = {}
        selected = selected_cell(data["cells"])
        for index, cell in enumerate(data["cells"]):
            if cell["date"] == old_date and cell["in_range"]:
                selected = index
                break

    def start_sync(reason: str) -> bool:
        nonlocal syncing, sync_future, status_message
        if db_path is None:
            status_message = "sync unavailable: no database path"
            return False
        if executor is None:
            status_message = "live sync is disabled"
            return False
        if syncing:
            status_message = "sync already running"
            return False
        syncing = True
        status_message = f"{reason}..."
        sync_future = executor.submit(
            collect_once,
            db_path=db_path,
            page_size=args.page_size,
            timeout=args.timeout,
            usage_timeout=args.usage_timeout,
        )
        return True

    def render() -> None:
        nonlocal cell_positions, scroll_offset
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        selected_info = data["cells"][selected]
        totals = data["totals"]
        last_scan = (stats.get("last_scan") or {}) if stats else {}
        live_state = "off"
        if live_enabled:
            live_state = "syncing" if syncing else f"next {interval_text((next_live_sync or time.time()) - time.time())}"
        balance = usage.get("total_available") if isinstance(usage, dict) else None
        balance_text = "N/A" if balance is None else f"{balance:,}"

        summary_items = [
            ("Tokens", human(totals.get("total_tokens", 0))),
            ("Input", human(totals.get("input_tokens", 0))),
            ("Output", human(totals.get("output_tokens", 0))),
            ("CacheR", human(totals.get("cache_read_tokens", 0))),
            ("Hit", pct(totals.get("cache_hit_rate"))),
            ("CacheC", human(totals.get("cache_create_tokens", 0))),
            ("Quota", human(totals.get("quota", 0))),
            ("NewAPI", newapi_money(totals.get("estimated_usd", 0))),
            ("Official", official_money(totals.get("official_usd", 0), fx)),
        ]

        if selected_info["date"] not in day_model_cache:
            day_model_cache[selected_info["date"]] = model_breakdown(rows, selected_info["date"], args.timezone, pricing)
        if selected_info["date"] not in day_ip_cache:
            day_ip_cache[selected_info["date"]] = ip_breakdown(rows, selected_info["date"], args.timezone, pricing)
        day_models = day_model_cache[selected_info["date"]]
        day_ips = day_ip_cache[selected_info["date"]]

        stacked_view = height < 32 or width < 132
        if stacked_view:
            lines = []
            title = "TokenCalendar"
            if data["token_name"]:
                title += f" / {data['token_name']}"
            if source_host:
                title += f" / {source_host}"
            append_wrapped(lines, title, width - 2)
            append_wrapped(lines, f"Range {data['range_start']} -> {data['range_end']} | heatmap={METRIC_LABELS[args.metric]} | stored_rows={(stats.get('stored_log_count') or len(rows)):,}", width - 2)
            if last_scan:
                continuity = "gap risk" if last_scan.get("gap_risk") else "no gap detected"
                append_wrapped(
                    lines,
                    f"Last scan {format_ts(last_scan.get('scanned_at'), args.timezone)} | fetched={last_scan.get('fetched_count', 0):,} | new={last_scan.get('inserted_count', 0):,} | duplicates={last_scan.get('duplicate_count', 0):,} | {continuity}",
                    width - 2,
                    indent="  ",
                )
            append_wrapped(lines, f"Live {live_state} | interval={interval_text(watch_interval)} | last={clock_time(last_live_sync, args.timezone)} | available={balance_text} | {fx_text(fx)}", width - 2)
            append_key_values(lines, summary_items, width - 2)
            lines.append("")
            lines.extend(text_heatmap_lines(data, metric_key, selected_info["date"], width - 2))
            lines.append("")
            lines.append(f"Day detail: {selected_info['date']}")
            append_key_values(
                lines,
                [
                    ("Tokens", human(selected_info["total_tokens"])),
                    ("Input", human(selected_info["input_tokens"])),
                    ("Output", human(selected_info["output_tokens"])),
                    ("Cache Read", human(selected_info["cache_read_tokens"])),
                    ("Cache Create", human(selected_info["cache_create_tokens"])),
                    ("Cache Hit", pct(selected_info["cache_hit_rate"])),
                    ("Quota", human(selected_info["quota"])),
                    ("NewAPI", newapi_money(selected_info["estimated_usd"])),
                    ("Official", official_money(selected_info["official_usd"], fx)),
                    ("Requests", f"{selected_info['requests']:,}"),
                ],
                width - 2,
            )
            lines.append("")
            lines.extend(stacked_model_lines(day_models, args.models, width - 2, fx))
            lines.append("")
            lines.extend(stacked_ip_lines(day_ips, args.ips, width - 2, not args.mask_ip, fx))
            viewport_h = max(1, height - 2)
            max_scroll = max(0, len(lines) - viewport_h)
            scroll_offset = min(max(scroll_offset, 0), max_scroll)
            for row, line in enumerate(lines[scroll_offset : scroll_offset + viewport_h]):
                attr = colors.get("title", 0) | curses.A_BOLD if row == 0 and scroll_offset == 0 else 0
                safe_addstr(stdscr, row, 1, line, attr)
            footer = f"scroll {scroll_offset}/{max_scroll} · arrows/hjkl move day · PgUp/PgDn scroll · 0-9 metric · r/f refresh · q"
            safe_addstr(stdscr, height - 1, 1, footer[: width - 2], colors.get("muted", 0))
            stdscr.refresh()
            return
        cell_gap = 1
        full_grid_w = max(1, (data["weeks"] - 1) * (2 + cell_gap) + 2)
        compact_grid_w = max(1, (data["weeks"] - 1) * (1 + cell_gap) + 1)
        compact = width < 130 or (6 + full_grid_w + 36 > width and 6 + compact_grid_w + 36 <= width)
        cell_w = 1 if compact else 2
        top_y = 7
        calendar_x = 1
        map_x = 6
        map_y = 9
        month_y = map_y - 1
        grid_w = max(1, (data["weeks"] - 1) * (cell_w + cell_gap) + cell_w)
        calendar_needed_w = map_x + grid_w + 2
        calendar_h = 13
        calendar_w = min(calendar_needed_w, MAX_CALENDAR_PANEL_WIDTH, max(40, width - 36))
        visible_grid_w = max(cell_w, calendar_x + calendar_w - 1 - map_x)
        visible_weeks = max(1, min(data["weeks"], (visible_grid_w + cell_gap) // (cell_w + cell_gap)))
        max_value = metric_max(data["cells"], metric_key)
        first_week = min(
            max(0, selected_info["week"] - visible_weeks + 1),
            max(0, data["weeks"] - visible_weeks),
        )
        last_visible_week = first_week + visible_weeks - 1

        title = "TokenCalendar"
        if data["token_name"]:
            title += f" / {data['token_name']}"
        if source_host:
            title += f" / {source_host}"
        if status_message:
            status_attr = colors.get("warn" if "failed" in status_message.lower() else "accent", 0)
            status_text = fit(status_message, max(8, width // 3))
            status_x = max(1, width - len(status_text) - 2)
            title_width = max(1, status_x - 2)
            safe_addstr(stdscr, 0, 1, fit(title, title_width), colors.get("title", 0) | curses.A_BOLD)
            safe_addstr(stdscr, 0, status_x, status_text, status_attr)
        else:
            safe_addstr(stdscr, 0, 1, fit(title, width - 2), colors.get("title", 0) | curses.A_BOLD)
        safe_addstr(
            stdscr,
            1,
            1,
            f"Range {data['range_start']} -> {data['range_end']}   heatmap={METRIC_LABELS[args.metric]}   stored_rows={(stats.get('stored_log_count') or len(rows)):,}",
            colors.get("muted", 0),
        )

        if last_scan:
            continuity = "gap risk" if last_scan.get("gap_risk") else "no gap detected"
            scan_attr = colors.get("danger" if last_scan.get("gap_risk") else "accent", 0)
            safe_addstr(
                stdscr,
                2,
                1,
                f"Last scan {format_ts(last_scan.get('scanned_at'), args.timezone)}  "
                f"fetched={last_scan.get('fetched_count', 0):,}  "
                f"new={last_scan.get('inserted_count', 0):,}  "
                f"duplicates={last_scan.get('duplicate_count', 0):,}  {continuity}",
                scan_attr,
            )
        safe_addstr(
            stdscr,
            3,
            1,
            fit(
                f"Live {live_state}  interval={interval_text(watch_interval)}  "
                f"last={clock_time(last_live_sync, args.timezone)}  available={balance_text}  {fx_text(fx)}",
                width - 2,
            ),
            colors.get("accent" if live_enabled else "muted", 0),
        )

        summary_end_y = draw_key_values(stdscr, 4, 1, width - 2, summary_items, colors)
        safe_addstr(
            stdscr,
            summary_end_y,
            1,
            fit("Remote scan window is 1000 rows; SQLite keeps every unique row seen by command-time and hourly scans.", width - 2),
            colors.get("muted", 0),
        )

        top_y = max(top_y, summary_end_y + 2)
        map_y = top_y + 2
        month_y = map_y - 1
        detail_y = top_y
        detail_x = calendar_x + calendar_w + 1
        detail_w = min(MAX_DETAIL_PANEL_WIDTH, max(32, width - detail_x - 1))
        detail_h = min(15, max(15, height - top_y - 7))
        upper_h = max(calendar_h, detail_h)
        draw_box(stdscr, top_y, calendar_x, calendar_h, calendar_w, f"Calendar: {METRIC_LABELS[args.metric]}", colors)
        month_slots = {}
        seen_months = set()
        for cell in data["cells"]:
            if not cell["in_range"] or not first_week <= cell["week"] <= last_visible_week:
                continue
            month_key = cell["date"][:7]
            if month_key in seen_months:
                continue
            seen_months.add(month_key)
            label = MONTHS[int(cell["date"][5:7]) - 1]
            month_slots[cell["week"]] = label[:1] if compact else label[:3]
        for week, label in month_slots.items():
            x = map_x + (week - first_week) * (cell_w + cell_gap)
            safe_addstr(stdscr, month_y, x, label, colors.get("muted", 0))

        if first_week > 0:
            safe_addstr(stdscr, map_y + 3, calendar_x + 1, "<", colors.get("muted", 0) | curses.A_BOLD)
        if last_visible_week < data["weeks"] - 1:
            safe_addstr(stdscr, map_y + 3, calendar_x + calendar_w - 2, ">", colors.get("muted", 0) | curses.A_BOLD)

        cell_positions = {}
        for weekday, label in enumerate(WEEKDAYS):
            safe_addstr(stdscr, map_y + weekday, 2, label[:3], colors.get("muted", 0))
        for idx, cell in enumerate(data["cells"]):
            if not cell["in_range"] or not first_week <= cell["week"] <= last_visible_week:
                continue
            y = map_y + cell["weekday"]
            x = map_x + (cell["week"] - first_week) * (cell_w + cell_gap)
            cell_level = level(metric_value(cell, metric_key), max_value)
            safe_addstr(
                stdscr,
                y,
                x,
                tui_cell_text(cell_level, cell_w, colors, args.ascii_heatmap),
                cell_attr(cell_level, idx == selected, colors),
            )
            for offset in range(cell_w):
                cell_positions[(y, x + offset)] = idx

        legend_y = map_y + 8
        safe_addstr(stdscr, legend_y, map_x, "Less", colors.get("muted", 0))
        lx = map_x + 6
        for lvl in range(5):
            safe_addstr(
                stdscr,
                legend_y,
                lx + lvl * (cell_w + 1),
                tui_cell_text(lvl, cell_w, colors, args.ascii_heatmap),
                cell_attr(lvl, False, colors),
            )
        safe_addstr(stdscr, legend_y, lx + 5 * (cell_w + 1) + 1, "More", colors.get("muted", 0))

        detail_content_y = detail_y + 2
        draw_box(stdscr, detail_y, detail_x, detail_h, detail_w, "Day detail", colors)
        safe_addstr(stdscr, detail_content_y, detail_x + 2, selected_info["date"], curses.A_BOLD)
        detail_rows = [
            ("Tokens", human(selected_info["total_tokens"])),
            ("Input", human(selected_info["input_tokens"])),
            ("Output", human(selected_info["output_tokens"])),
            ("Cache Read", human(selected_info["cache_read_tokens"])),
            ("Cache Create", human(selected_info["cache_create_tokens"])),
            ("Cache Hit", pct(selected_info["cache_hit_rate"])),
            ("Quota", human(selected_info["quota"])),
            ("NewAPI", newapi_money(selected_info["estimated_usd"])),
            ("Official", official_money(selected_info["official_usd"], fx)),
            ("Requests", f"{selected_info['requests']:,}"),
        ]
        for offset, (label, value) in enumerate(detail_rows, start=1):
            safe_addstr(stdscr, detail_content_y + offset, detail_x + 2, label, colors.get("muted", 0))
            safe_addstr(stdscr, detail_content_y + offset, detail_x + 15, value, curses.A_BOLD)

        tables_y = top_y + upper_h + 1
        tables_h = min(8, height - tables_y - 2)
        if tables_h >= 4:
            available_w = width - 2
            ip_box_w = min(MAX_IP_TABLE_WIDTH + 4, max(80, available_w - (MAX_MODEL_TABLE_WIDTH + 4) - 1))
            model_box_w = min(MAX_MODEL_TABLE_WIDTH + 4, available_w - ip_box_w - 1)
            split_tables = model_box_w >= 84 and ip_box_w >= 80 and day_ips
            if split_tables:
                ip_box_x = model_box_w + 2
                ip_box_w = min(ip_box_w, width - ip_box_x - 1)
            else:
                model_box_w = min(MAX_MODEL_TABLE_WIDTH + 4, available_w)
                ip_box_x = 0
                ip_box_w = 0

            draw_box(stdscr, tables_y, 1, tables_h, model_box_w, "Top models", colors)
            shown = data["top_models"][: args.models]
            table_width = min(MAX_MODEL_TABLE_WIDTH, model_box_w - 4)
            safe_addstr(stdscr, tables_y + 1, 3, model_table_header(table_width), colors.get("muted", 0) | curses.A_BOLD)
            for offset, item in enumerate(shown[: max(0, tables_h - 3)]):
                safe_addstr(stdscr, tables_y + 2 + offset, 3, model_table_row(item, table_width, fx))

            if split_tables and ip_box_w >= 44:
                draw_box(stdscr, tables_y, ip_box_x, tables_h, ip_box_w, "Day IPs", colors)
                ip_table_w = min(MAX_IP_TABLE_WIDTH, ip_box_w - 4)
                safe_addstr(stdscr, tables_y + 1, ip_box_x + 2, ip_table_header(ip_table_w), colors.get("muted", 0) | curses.A_BOLD)
                for offset, item in enumerate(day_ips[: max(0, tables_h - 3)]):
                    safe_addstr(
                        stdscr,
                        tables_y + 2 + offset,
                        ip_box_x + 2,
                        ip_table_row(item, ip_table_w, not args.mask_ip, fx),
                    )
        else:
            safe_addstr(stdscr, height - 2, 1, "Top models hidden; resize taller for the full table.", colors.get("muted", 0))

        message = (
            f"mouse/arrows/hjkl · 0-9 metric · r/f refresh · "
            f"space live {'off' if live_enabled else 'on'} · q"
        )
        safe_addstr(stdscr, height - 1, 1, message[: width - 2], colors.get("muted", 0))
        stdscr.refresh()

    try:
        while True:
            now = time.time()
            if syncing and sync_future is not None and sync_future.done():
                try:
                    result = sync_future.result()
                    fx = load_fx(args)
                    reload_context(load_dashboard_context(db_path, args, pricing, fx))
                    last_live_sync = time.time()
                    next_live_sync = last_live_sync + watch_interval if live_enabled else None
                    status_message = f"synced +{result['inserted_count']:,}"
                except Exception as exc:
                    last_live_sync = time.time()
                    next_live_sync = last_live_sync + watch_interval if live_enabled else None
                    status_message = f"sync failed: {exc}"
                finally:
                    syncing = False
                    sync_future = None

            if live_enabled and not syncing and next_live_sync is not None and now >= next_live_sync:
                start_sync("auto sync")

            render()
            key = stdscr.getch()
            if key == -1:
                continue
            if key in (ord("q"), ord("Q"), 27):
                break
            if key in (curses.KEY_LEFT, ord("h")):
                selected = move_selection(data, selected, delta_week=-1)
                scroll_offset = 0
            elif key in (curses.KEY_RIGHT, ord("l")):
                selected = move_selection(data, selected, delta_week=1)
                scroll_offset = 0
            elif key in (curses.KEY_UP, ord("k")):
                selected = move_selection(data, selected, delta_day=-1)
                scroll_offset = 0
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = move_selection(data, selected, delta_day=1)
                scroll_offset = 0
            elif key in (curses.KEY_NPAGE, ord("d")):
                scroll_offset += 8
            elif key in (curses.KEY_PPAGE, ord("u")):
                scroll_offset = max(0, scroll_offset - 8)
            elif key == ord("g"):
                for index, cell in enumerate(data["cells"]):
                    if cell["in_range"]:
                        selected = index
                        break
                scroll_offset = 0
            elif key == ord("G"):
                selected = selected_cell(data["cells"])
                scroll_offset = 0
            elif 0 <= key <= 255 and chr(key) in METRIC_HOTKEYS:
                args.metric = METRIC_HOTKEYS[chr(key)]
                metric_key = METRICS[args.metric]
                status_message = f"heatmap: {METRIC_LABELS[args.metric]}"
                scroll_offset = 0
            elif key in (ord("r"), ord("R"), ord("f"), ord("F")) and db_path is not None:
                if executor is None:
                    executor = ThreadPoolExecutor(max_workers=1)
                start_sync("fetching")
                next_live_sync = time.time() + watch_interval if live_enabled else None
            elif key == ord(" "):
                live_enabled = not live_enabled
                if live_enabled:
                    if executor is None:
                        executor = ThreadPoolExecutor(max_workers=1)
                    next_live_sync = time.time() + watch_interval
                    status_message = "live sync on"
                else:
                    next_live_sync = None
                    status_message = "live sync off"
            elif key == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, _ = curses.getmouse()
                except curses.error:
                    continue
                if (my, mx) in cell_positions:
                    selected = cell_positions[(my, mx)]
    finally:
        if sync_future is not None and not sync_future.done():
            sync_future.cancel()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        try:
            print("\033[?1003l", end="", flush=True)
        except Exception:
            pass


def print_dashboard(args: argparse.Namespace) -> None:
    db_path = Path(args.db)
    interactive = not args.plain and sys.stdout.isatty()
    status_parts = []

    if args.no_refresh and not db_path.exists():
        raise SystemExit(f"Database does not exist: {db_path}. Run without --no-refresh first.")

    with ThreadPoolExecutor(max_workers=3) as executor:
        pricing_future = executor.submit(
            PricingCatalog.load,
            Path(args.pricing_cache),
            refresh=args.refresh_pricing,
            offline=args.offline_pricing,
            timeout=args.pricing_timeout,
        )
        fx_future = executor.submit(load_fx, args)
        collect_future = None
        if not args.no_refresh:
            collect_future = executor.submit(
                collect_once,
                db_path=db_path,
                page_size=args.page_size,
                timeout=args.timeout,
                usage_timeout=args.usage_timeout,
            )

        if collect_future is not None:
            try:
                result = collect_future.result()
                status_parts.append(f"synced +{result['inserted_count']:,}")
                if args.plain or not sys.stdout.isatty():
                    print(
                        f"Refreshed: {result['inserted_count']:,} inserted, "
                        f"{result['duplicate_count']:,} duplicates, {result['fetched_count']:,} fetched"
                    )
            except Exception as exc:
                if not db_path.exists():
                    raise
                status_parts.append(f"sync failed: {exc}")
                if args.plain or not sys.stdout.isatty():
                    print(status_parts[-1])

        try:
            pricing = pricing_future.result()
        except Exception as exc:
            pricing = PricingCatalog()
            status_parts.append(f"pricing load failed: {exc}")
        if pricing.status.error:
            status_parts.append(pricing.status.error)

        try:
            fx = fx_future.result()
        except Exception as exc:
            fx = FxRate(date="", usd_cny=0.0, source="unavailable", error=f"FX load failed: {exc}")
        if fx.error:
            status_parts.append(fx.error)

    status_message = "; ".join(part for part in status_parts if part)
    context = load_dashboard_context(db_path, args, pricing, fx)

    if interactive:
        context["db_path"] = db_path
        context["status_message"] = status_message
        curses.wrapper(draw_tui, context, args)
        return

    rows = context["rows"]
    usage = context["usage"]
    source_host = context["source_host"]
    stats = context["stats"]
    data = context["data"]
    pricing = context["pricing"]
    fx = context["fx"]
    metric_key = METRICS[args.metric]
    use_color = color_enabled(args.color)
    columns = shutil.get_terminal_size((120, 24)).columns
    compact = args.compact or columns < 130

    title = "TokenCalendar"
    if data["token_name"]:
        title += f" / {data['token_name']}"
    if source_host:
        title += f" / {source_host}"
    print(paint_text(title, "1;36", use_color))
    print(
        f"Range {data['range_start']} -> {data['range_end']}  "
        f"heatmap={METRIC_LABELS[args.metric]}  stored_rows={(stats.get('stored_log_count') or len(rows)):,}"
    )

    last_scan = (stats.get("last_scan") or {}) if stats else {}
    if last_scan:
        continuity = "gap risk" if last_scan.get("gap_risk") else "no gap detected"
        print(
            f"Last scan {format_ts(last_scan.get('scanned_at'), args.timezone)}  "
            f"fetched={last_scan.get('fetched_count', 0):,}  "
            f"new={last_scan.get('inserted_count', 0):,}  "
            f"duplicates={last_scan.get('duplicate_count', 0):,}  {continuity}"
        )

    totals = data["totals"]
    balance = usage.get("total_available")
    balance_text = "N/A" if balance is None else f"{balance:,}"
    print(
        f"Tokens {human(totals.get('total_tokens', 0))}  "
        f"input {human(totals.get('input_tokens', 0))}  "
        f"output {human(totals.get('output_tokens', 0))}  "
        f"cache_create {human(totals.get('cache_create_tokens', 0))}  "
        f"cache_read {human(totals.get('cache_read_tokens', 0))}  "
        f"cache_hit {pct(totals.get('cache_hit_rate'))}  "
        f"quota {human(totals.get('quota', 0))}  "
        f"newapi {newapi_money(totals.get('estimated_usd', 0))}  "
        f"official {official_money(totals.get('official_usd', 0), fx)}  "
        f"available {balance_text}"
    )
    if data["first_day"] and data["last_day"]:
        print(f"Visible log span {data['first_day']} -> {data['last_day']}")
    print(
        "Official price model: gpt-5.5 for gpt-5.5 and gpt-5.5-openai-compact "
        f"(input ${pricing.gpt55_price.input_cost_per_token * 1_000_000:.2f}/M, "
        f"cached ${pricing.gpt55_price.cache_read_input_token_cost * 1_000_000:.2f}/M, "
        f"output ${pricing.gpt55_price.output_cost_per_token * 1_000_000:.2f}/M)."
    )
    if pricing.gpt55_price.long_context_threshold:
        print(
            f"Official long-context tier: prompt > {pricing.gpt55_price.long_context_threshold:,} tokens uses "
            f"input ${((pricing.gpt55_price.input_cost_per_token_above_threshold or pricing.gpt55_price.input_cost_per_token) * 1_000_000):.2f}/M, "
            f"cached ${((pricing.gpt55_price.cache_read_input_token_cost_above_threshold or pricing.gpt55_price.cache_read_input_token_cost) * 1_000_000):.2f}/M, "
            f"output ${((pricing.gpt55_price.output_cost_per_token_above_threshold or pricing.gpt55_price.output_cost_per_token) * 1_000_000):.2f}/M."
        )
    price_age = format_ts(pricing.status.refreshed_at, args.timezone) if pricing.status.refreshed_at else "embedded"
    print(f"Official price source: {pricing.status.source}  refreshed={price_age}")
    print(f"Official CNY conversion: {fx_text(fx)}  source={fx.source}")
    if pricing.status.error:
        print(pricing.status.error)
    if fx.error:
        print(fx.error)
    print("Remote endpoint returns the latest 1000 rows per scan; SQLite accumulates rows seen since collection started.")
    print("")

    for line in render_heatmap(data, metric_key, use_color, compact):
        print(line)

    print("")
    for line in render_models(data["top_models"], args.models, width=columns - 2, fx=fx):
        print(line)

    ip_date = data["last_day"].isoformat() if data["last_day"] else None
    if ip_date:
        print("")
        print(f"IP distribution for {ip_date}")
        for line in render_ips(
            ip_breakdown(rows, ip_date, args.timezone, pricing),
            args.ips,
            width=columns - 2,
            show_full=not args.mask_ip,
            fx=fx,
        )[1:]:
            print(line)


def main() -> None:
    parser = argparse.ArgumentParser(description="Show a terminal heatmap for New API token usage.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path.")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--metric", choices=sorted(METRICS), default="tokens")
    parser.add_argument("--models", type=int, default=6, help="Number of top models to show.")
    parser.add_argument("--ips", type=int, default=6, help="Number of IP rows to show.")
    parser.add_argument("--mask-ip", action="store_true", help="Mask IP addresses in terminal output.")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--timeout", type=float, default=6.0, help="Remote request timeout per remote request in seconds.")
    parser.add_argument("--usage-timeout", type=float, default=2.0, help="Short timeout for the optional balance request.")
    parser.add_argument("--pricing-cache", default=str(DEFAULT_PRICING_CACHE_PATH), help="Cached LiteLLM pricing JSON path.")
    parser.add_argument("--pricing-timeout", type=float, default=3.0, help="Pricing refresh timeout in seconds.")
    parser.add_argument("--refresh-pricing", action="store_true", help="Refresh model pricing from LiteLLM before rendering.")
    parser.add_argument("--offline-pricing", action="store_true", help="Use cached or embedded pricing without network access.")
    parser.add_argument("--fx-cache", default=str(DEFAULT_FX_CACHE_PATH), help="Cached USD/CNY historical rate JSON path.")
    parser.add_argument("--fx-timeout", type=float, default=4.0, help="USD/CNY historical rate request timeout in seconds.")
    parser.add_argument("--offline-fx", action="store_true", help="Use cached yesterday USD/CNY rate without network access.")
    parser.add_argument("--no-refresh", action="store_true", help="Display the local archive without fetching first.")
    parser.add_argument("--watch-interval", type=float, default=300.0, help="Interactive auto-refresh interval in seconds.")
    parser.add_argument("--no-watch", action="store_true", help="Disable interactive auto-refresh.")
    parser.add_argument("--plain", action="store_true", help="Print a non-interactive terminal view.")
    parser.add_argument("--compact", action="store_true", help="Use one-character cells.")
    parser.add_argument("--ascii-heatmap", action="store_true", help="Render TUI heatmap cells with visible text characters.")
    parser.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    args = parser.parse_args()

    print_dashboard(args)


if __name__ == "__main__":
    main()
