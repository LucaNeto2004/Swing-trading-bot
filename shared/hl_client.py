"""Lightweight HyperLiquid info API wrapper.

Uses the public `https://api.hyperliquid.xyz/info` endpoint — no signing needed
for the read-only calls exposed here (positions, balance, fills, funding,
candles). For authenticated actions (order placement) use the official
`hyperliquid-python-sdk` directly from the bot's execution layer.

Both the commodities-bot and crypto-bot can import this module and share the
same HL request path.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
import pandas as pd

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
DEFAULT_TIMEOUT = 10.0
MAX_RETRIES = 4
RETRY_BACKOFF = 0.5


class HLError(RuntimeError):
    """Raised on any HL API failure the caller should surface."""


async def _post(payload: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """POST with exponential backoff on 429/5xx and connection errors."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.post(HL_INFO_URL, json=payload)
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    raise httpx.HTTPStatusError(
                        f"HL {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt == MAX_RETRIES - 1:
                    raise HLError(f"HL request failed after {MAX_RETRIES} attempts: {exc}") from exc
                await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
    raise HLError("unreachable")


def _resolve_address(address: str | None) -> str:
    addr = address or os.environ.get("HL_WALLET_ADDRESS")
    if not addr:
        raise HLError(
            "HyperLiquid wallet address not provided. Pass `address=...` or set HL_WALLET_ADDRESS."
        )
    return addr


async def get_balance(address: str | None = None) -> dict:
    """Return account equity, available margin, total unrealised PnL."""
    addr = _resolve_address(address)
    data = await _post({"type": "clearinghouseState", "user": addr})
    margin = data.get("marginSummary", {}) or {}
    return {
        "account_value": float(margin.get("accountValue", 0) or 0),
        "total_ntl_pos": float(margin.get("totalNtlPos", 0) or 0),
        "total_raw_usd": float(margin.get("totalRawUsd", 0) or 0),
        "total_margin_used": float(margin.get("totalMarginUsed", 0) or 0),
        "available_margin": (
            float(margin.get("accountValue", 0) or 0)
            - float(margin.get("totalMarginUsed", 0) or 0)
        ),
        "unrealised_pnl": sum(
            float(p.get("position", {}).get("unrealizedPnl", 0) or 0)
            for p in data.get("assetPositions", [])
        ),
    }


async def get_positions(address: str | None = None) -> list[dict]:
    """Return open positions with entry, size, unrealised PnL, liquidation."""
    addr = _resolve_address(address)
    data = await _post({"type": "clearinghouseState", "user": addr})
    out = []
    for ap in data.get("assetPositions", []):
        pos = ap.get("position", {}) or {}
        size = float(pos.get("szi", 0) or 0)
        if size == 0:
            continue
        out.append({
            "coin": pos.get("coin"),
            "size": size,
            "side": "long" if size > 0 else "short",
            "entry_price": float(pos.get("entryPx", 0) or 0),
            "unrealised_pnl": float(pos.get("unrealizedPnl", 0) or 0),
            "liquidation_price": float(pos.get("liquidationPx", 0) or 0),
            "leverage": pos.get("leverage", {}),
        })
    return out


async def get_recent_fills(address: str | None = None, limit: int = 50) -> list[dict]:
    """Return recent fills. HL returns newest first; we trim to `limit`."""
    addr = _resolve_address(address)
    data = await _post({"type": "userFills", "user": addr})
    fills = data if isinstance(data, list) else []
    out = []
    for f in fills[:limit]:
        out.append({
            "timestamp_ms": int(f.get("time", 0) or 0),
            "coin": f.get("coin"),
            "side": "buy" if f.get("side") == "B" else "sell",
            "price": float(f.get("px", 0) or 0),
            "size": float(f.get("sz", 0) or 0),
            "fee": float(f.get("fee", 0) or 0),
            "closed_pnl": float(f.get("closedPnl", 0) or 0),
            "oid": f.get("oid"),
            "tid": f.get("tid"),
        })
    return out


async def get_funding_rates(instruments: list[str]) -> dict[str, float]:
    """Return current funding rate for each requested coin."""
    data = await _post({"type": "metaAndAssetCtxs"})
    if not isinstance(data, list) or len(data) < 2:
        return {coin: 0.0 for coin in instruments}
    meta, ctxs = data[0], data[1]
    universe = meta.get("universe", [])
    name_to_idx = {u.get("name"): i for i, u in enumerate(universe)}
    out = {}
    for coin in instruments:
        idx = name_to_idx.get(coin)
        if idx is None or idx >= len(ctxs):
            out[coin] = 0.0
            continue
        out[coin] = float(ctxs[idx].get("funding", 0) or 0)
    return out


async def get_market_context(instruments: list[str]) -> dict[str, dict]:
    """Return live market context per coin: funding, open interest, premium,
    mark/oracle prices, 24h volume. Uses the same metaAndAssetCtxs call as
    get_funding_rates so both can be served from one request if needed."""
    data = await _post({"type": "metaAndAssetCtxs"})
    if not isinstance(data, list) or len(data) < 2:
        return {coin: {} for coin in instruments}
    meta, ctxs = data[0], data[1]
    universe = meta.get("universe", [])
    name_to_idx = {u.get("name"): i for i, u in enumerate(universe)}
    out = {}
    for coin in instruments:
        idx = name_to_idx.get(coin)
        if idx is None or idx >= len(ctxs):
            out[coin] = {}
            continue
        c = ctxs[idx]
        out[coin] = {
            "funding": float(c.get("funding", 0) or 0),
            "open_interest": float(c.get("openInterest", 0) or 0),
            "premium": float(c.get("premium", 0) or 0),
            "mark_px": float(c.get("markPx", 0) or 0),
            "oracle_px": float(c.get("oraclePx", 0) or 0),
            "day_ntl_vlm": float(c.get("dayNtlVlm", 0) or 0),
        }
    return out


async def get_funding_history(
    coin: str, start_ms: int, end_ms: int | None = None,
) -> pd.DataFrame:
    """Return hourly funding-rate history for one coin as a DataFrame with
    columns [timestamp, funding_rate, premium]. HL emits one entry per hour."""
    import time
    end = end_ms if end_ms is not None else int(time.time() * 1000)
    payload = {
        "type": "fundingHistory", "coin": coin,
        "startTime": start_ms, "endTime": end,
    }
    data = await _post(payload)
    if not isinstance(data, list) or not data:
        return pd.DataFrame(columns=["timestamp", "funding_rate", "premium"])
    rows = [{
        "timestamp": pd.to_datetime(int(d.get("time", 0)), unit="ms", utc=True),
        "funding_rate": float(d.get("fundingRate", 0) or 0),
        "premium": float(d.get("premium", 0) or 0),
    } for d in data]
    return pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)


_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "8h": 28_800_000,
    "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000,
    "1M": 2_592_000_000,
}


async def get_candles(
    instrument: str,
    interval: str,
    limit: int = 100,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> pd.DataFrame:
    """Return OHLCV candles as a DataFrame indexed by UTC timestamp.

    `interval` must be one of HL's supported intervals: 1m, 3m, 5m, 15m, 30m,
    1h, 2h, 4h, 8h, 12h, 1d, 3d, 1w, 1M.

    If start_ms/end_ms are provided, the explicit window is used (historical
    replay). Otherwise the window is computed as the last `limit` intervals
    ending now (live use).
    """
    interval_ms = _INTERVAL_MS.get(interval)
    if interval_ms is None:
        raise HLError(f"Unsupported HL interval: {interval}")

    import time
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    if start_ms is None:
        start_ms = end_ms - (interval_ms * (limit + 5))

    payload = {
        "type": "candleSnapshot",
        "req": {"coin": instrument, "interval": interval,
                "startTime": start_ms, "endTime": end_ms},
    }
    data = await _post(payload)
    if not isinstance(data, list):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    rows = []
    for c in data[-limit:]:
        rows.append({
            "timestamp": pd.to_datetime(int(c.get("t", 0)), unit="ms", utc=True),
            "open": float(c.get("o", 0) or 0),
            "high": float(c.get("h", 0) or 0),
            "low":  float(c.get("l", 0) or 0),
            "close": float(c.get("c", 0) or 0),
            "volume": float(c.get("v", 0) or 0),
        })
    df = pd.DataFrame(rows).set_index("timestamp") if rows else pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"]
    )
    return df


# Sync convenience wrappers for callers that don't want to deal with asyncio.
def sync_get_balance(address: str | None = None) -> dict:
    return asyncio.run(get_balance(address))


def sync_get_positions(address: str | None = None) -> list[dict]:
    return asyncio.run(get_positions(address))


def sync_get_recent_fills(address: str | None = None, limit: int = 50) -> list[dict]:
    return asyncio.run(get_recent_fills(address, limit))


def sync_get_funding_rates(instruments: list[str]) -> dict[str, float]:
    return asyncio.run(get_funding_rates(instruments))


def sync_get_market_context(instruments: list[str]) -> dict[str, dict]:
    return asyncio.run(get_market_context(instruments))


def sync_get_funding_history(coin: str, start_ms: int, end_ms: int | None = None) -> pd.DataFrame:
    return asyncio.run(get_funding_history(coin, start_ms, end_ms))


def sync_get_candles(instrument: str, interval: str, limit: int = 100,
                     start_ms: int | None = None, end_ms: int | None = None) -> pd.DataFrame:
    return asyncio.run(get_candles(instrument, interval, limit, start_ms, end_ms))


if __name__ == "__main__":  # pragma: no cover
    # Quick smoke test — candles don't need an address.
    df = sync_get_candles("ETH", "1h", 20)
    print(f"ETH 1h candles ({len(df)} rows):")
    print(df.tail())

    funding = sync_get_funding_rates(["ETH", "HYPE", "BTC"])
    print("\nFunding rates:", funding)
