from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


LITELLM_PRICING_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
DEFAULT_PRICING_CACHE_PATH = Path("data/model_prices_and_context_window.json")
DEFAULT_REFRESH_INTERVAL = 24 * 60 * 60


@dataclass(frozen=True)
class TokenPrice:
    input_cost_per_token: float
    output_cost_per_token: float
    cache_read_input_token_cost: float
    cache_creation_input_token_cost: float
    source: str
    long_context_threshold: int | None = None
    input_cost_per_token_above_threshold: float | None = None
    output_cost_per_token_above_threshold: float | None = None
    cache_read_input_token_cost_above_threshold: float | None = None
    cache_creation_input_token_cost_above_threshold: float | None = None


@dataclass(frozen=True)
class PricingStatus:
    source: str
    refreshed_at: int | None
    error: str = ""


GPT55_PRICE = TokenPrice(
    input_cost_per_token=5.0 / 1_000_000,
    output_cost_per_token=30.0 / 1_000_000,
    cache_read_input_token_cost=0.5 / 1_000_000,
    cache_creation_input_token_cost=5.0 / 1_000_000,
    source="embedded gpt-5.5 price",
    long_context_threshold=272_000,
    input_cost_per_token_above_threshold=10.0 / 1_000_000,
    output_cost_per_token_above_threshold=45.0 / 1_000_000,
    cache_read_input_token_cost_above_threshold=1.0 / 1_000_000,
    cache_creation_input_token_cost_above_threshold=10.0 / 1_000_000,
)


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_gpt55_price(payload: dict, source: str) -> TokenPrice | None:
    row = payload.get("gpt-5.5")
    if not isinstance(row, dict):
        return None
    input_cost = _as_float(row.get("input_cost_per_token"))
    output_cost = _as_float(row.get("output_cost_per_token"))
    cache_read = _as_float(row.get("cache_read_input_token_cost"))
    cache_create = _as_float(row.get("cache_creation_input_token_cost"), input_cost)
    long_context_threshold = None
    above_suffix = ""
    if any(key.endswith("_above_272k_tokens") for key in row):
        long_context_threshold = 272_000
        above_suffix = "_above_272k_tokens"
    elif any(key.endswith("_above_200k_tokens") for key in row):
        long_context_threshold = 200_000
        above_suffix = "_above_200k_tokens"
    if input_cost <= 0 or output_cost <= 0:
        return None
    return TokenPrice(
        input_cost_per_token=input_cost,
        output_cost_per_token=output_cost,
        cache_read_input_token_cost=cache_read,
        cache_creation_input_token_cost=cache_create,
        source=source,
        long_context_threshold=long_context_threshold,
        input_cost_per_token_above_threshold=_as_float(
            row.get(f"input_cost_per_token{above_suffix}"),
            0.0,
        )
        or None,
        output_cost_per_token_above_threshold=_as_float(
            row.get(f"output_cost_per_token{above_suffix}"),
            0.0,
        )
        or None,
        cache_read_input_token_cost_above_threshold=_as_float(
            row.get(f"cache_read_input_token_cost{above_suffix}"),
            0.0,
        )
        or None,
        cache_creation_input_token_cost_above_threshold=_as_float(
            row.get(f"cache_creation_input_token_cost{above_suffix}"),
            0.0,
        )
        or None,
    )


class PricingCatalog:
    def __init__(
        self,
        price: TokenPrice = GPT55_PRICE,
        status: PricingStatus | None = None,
    ) -> None:
        self.gpt55_price = price
        self.status = status or PricingStatus(source=price.source, refreshed_at=None)

    @classmethod
    def load(
        cls,
        cache_path: Path = DEFAULT_PRICING_CACHE_PATH,
        *,
        refresh: bool = False,
        offline: bool = False,
        timeout: float = 3.0,
        max_age_seconds: int = DEFAULT_REFRESH_INTERVAL,
    ) -> "PricingCatalog":
        cache_path = Path(cache_path)
        cached = cls._load_cached(cache_path)
        now = int(time.time())

        if cached and offline:
            return cached

        stale = (
            cached is None
            or cached.status.refreshed_at is None
            or now - cached.status.refreshed_at >= max_age_seconds
        )
        if not offline and (refresh or stale):
            try:
                refreshed = cls._fetch_litellm(cache_path, timeout=timeout)
                if refreshed:
                    return refreshed
            except Exception as exc:
                if cached:
                    return cls(
                        cached.gpt55_price,
                        PricingStatus(
                            source=cached.status.source,
                            refreshed_at=cached.status.refreshed_at,
                            error=f"pricing refresh failed: {exc}",
                        ),
                    )
                return cls(
                    GPT55_PRICE,
                    PricingStatus(
                        source=GPT55_PRICE.source,
                        refreshed_at=None,
                        error=f"pricing refresh failed: {exc}",
                    ),
                )

        if cached:
            return cached
        return cls(GPT55_PRICE, PricingStatus(source=GPT55_PRICE.source, refreshed_at=None))

    @classmethod
    def _load_cached(cls, cache_path: Path) -> "PricingCatalog | None":
        if not cache_path.exists():
            return None
        payload = json.loads(cache_path.read_text())
        price = _parse_gpt55_price(payload, f"cached LiteLLM pricing: {cache_path}")
        if not price:
            return None
        refreshed_at = None
        try:
            refreshed_at = int(cache_path.stat().st_mtime)
        except OSError:
            pass
        return cls(price, PricingStatus(source=price.source, refreshed_at=refreshed_at))

    @classmethod
    def _fetch_litellm(cls, cache_path: Path, timeout: float = 3.0) -> "PricingCatalog | None":
        request = urllib.request.Request(
            LITELLM_PRICING_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": "TokenCalendar/0.3",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code} from LiteLLM pricing") from exc

        payload = json.loads(body)
        price = _parse_gpt55_price(payload, f"LiteLLM pricing: {LITELLM_PRICING_URL}")
        if not price:
            raise RuntimeError("LiteLLM pricing did not include gpt-5.5")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return cls(price, PricingStatus(source=price.source, refreshed_at=int(time.time())))

    def price_for_model(self, model_name: str) -> TokenPrice | None:
        model = (model_name or "").lower().strip()
        if model in {"gpt-5.5", "gpt-5.5-openai-compact"}:
            return self.gpt55_price
        return None

    def official_cost_usd(
        self,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_create_tokens: int,
    ) -> float:
        price = self.price_for_model(model_name)
        if price is None:
            return 0.0
        prompt_total = input_tokens + cache_read_tokens + cache_create_tokens
        long_context = bool(
            price.long_context_threshold
            and prompt_total > price.long_context_threshold
        )
        input_rate = price.input_cost_per_token
        output_rate = price.output_cost_per_token
        cache_read_rate = price.cache_read_input_token_cost
        cache_create_rate = price.cache_creation_input_token_cost
        if long_context:
            input_rate = price.input_cost_per_token_above_threshold or input_rate
            output_rate = price.output_cost_per_token_above_threshold or output_rate
            cache_read_rate = price.cache_read_input_token_cost_above_threshold or cache_read_rate
            cache_create_rate = price.cache_creation_input_token_cost_above_threshold or input_rate
        return (
            input_tokens * input_rate
            + output_tokens * output_rate
            + cache_read_tokens * cache_read_rate
            + cache_create_tokens * cache_create_rate
        )
