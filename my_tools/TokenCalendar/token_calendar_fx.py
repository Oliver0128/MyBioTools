from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULT_FX_CACHE_PATH = Path("data/usd_cny_rate.json")


@dataclass(frozen=True)
class FxRate:
    date: str
    usd_cny: float
    source: str
    fetched_at: int | None = None
    error: str = ""


def target_fx_date(timezone: str = "Asia/Shanghai") -> str:
    today = datetime.now(ZoneInfo(timezone)).date()
    return (today - timedelta(days=1)).isoformat()


def _request_json(url: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "TokenCalendar/0.3",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_frankfurter_v2(payload: dict | list, source: str) -> FxRate | None:
    row = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(row, dict):
        return None
    rate = row.get("rate")
    date = row.get("date")
    if not rate or not date:
        return None
    return FxRate(date=str(date), usd_cny=float(rate), source=source, fetched_at=int(time.time()))


def _parse_frankfurter_v1(payload: dict, source: str) -> FxRate | None:
    rate = (payload.get("rates") or {}).get("CNY")
    date = payload.get("date")
    if not rate or not date:
        return None
    return FxRate(date=str(date), usd_cny=float(rate), source=source, fetched_at=int(time.time()))


def _parse_currency_api(payload: dict, source: str) -> FxRate | None:
    rate = (payload.get("usd") or {}).get("cny")
    date = payload.get("date")
    if not rate or not date:
        return None
    return FxRate(date=str(date), usd_cny=float(rate), source=source, fetched_at=int(time.time()))


def _load_cached(cache_path: Path, date: str) -> FxRate | None:
    if not cache_path.exists():
        return None
    payload = json.loads(cache_path.read_text())
    if payload.get("date") != date:
        return None
    rate = payload.get("usd_cny")
    if not rate:
        return None
    return FxRate(
        date=str(payload["date"]),
        usd_cny=float(rate),
        source=str(payload.get("source") or f"cache: {cache_path}"),
        fetched_at=int(payload.get("fetched_at") or cache_path.stat().st_mtime),
    )


def _write_cache(cache_path: Path, rate: FxRate) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "date": rate.date,
                "usd_cny": rate.usd_cny,
                "source": rate.source,
                "fetched_at": rate.fetched_at or int(time.time()),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def load_usd_cny_rate(
    *,
    timezone: str = "Asia/Shanghai",
    cache_path: Path = DEFAULT_FX_CACHE_PATH,
    timeout: float = 4.0,
    offline: bool = False,
) -> FxRate:
    date = target_fx_date(timezone)
    cache_path = Path(cache_path)
    cached = _load_cached(cache_path, date)
    if cached:
        return cached
    if offline:
        return FxRate(date=date, usd_cny=0.0, source=f"missing cache: {cache_path}", error="FX cache is missing")

    urls = [
        (
            f"https://api.frankfurter.dev/v2/rates?date={date}&base=USD&quotes=CNY",
            _parse_frankfurter_v2,
        ),
        (
            f"https://api.frankfurter.app/{date}?from=USD&to=CNY",
            _parse_frankfurter_v1,
        ),
        (
            f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{date}/v1/currencies/usd.json",
            _parse_currency_api,
        ),
    ]
    last_error = ""
    for url, parser in urls:
        try:
            rate = parser(_request_json(url, timeout), url)
            if rate and rate.date == date and rate.usd_cny > 0:
                _write_cache(cache_path, rate)
                return rate
            if rate:
                last_error = f"{url} returned {rate.date}, expected {date}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
    return FxRate(date=date, usd_cny=0.0, source="unavailable", error=f"FX fetch failed: {last_error}")
