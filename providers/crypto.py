from __future__ import annotations

import re

import httpx

from ..models import QuoteResult


def normalize_crypto_symbol(raw: str) -> str:
    """Normalize symbol like BTC, btc/usdt, BTCUSDT -> BTC."""
    s = raw.strip().upper()
    s = re.sub(r"[-_/]?(USDT|USD|USDC)$", "", s)
    return s.strip("-_/")


class CryptoProvider:
    """Free public crypto spot and perpetual swap quote provider with multi-exchange fallback."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def get_spot(self, symbol: str) -> QuoteResult | None:
        base = normalize_crypto_symbol(symbol)
        if not base or not re.fullmatch(r"[A-Z0-9]{1,20}", base):
            return None

        for fn in (self._okx_spot, self._binance_vision_spot, self._gate_spot):
            result = await fn(base)
            if result is not None:
                return result
        return None

    async def get_perp(self, symbol: str) -> QuoteResult | None:
        base = normalize_crypto_symbol(symbol)
        if not base or not re.fullmatch(r"[A-Z0-9]{1,20}", base):
            return None

        for fn in (self._okx_perp, self._gate_perp, self._binance_futures_perp):
            result = await fn(base)
            if result is not None:
                return result
        return None

    async def get_quote(
        self,
        symbol: str,
        contract_type: str = "spot",
        allow_downgrade: bool = True,
    ) -> QuoteResult | None:
        if contract_type == "perp":
            return await self.get_perp(symbol)

        spot = await self.get_spot(symbol)
        if spot is not None:
            return spot

        if allow_downgrade:
            perp = await self.get_perp(symbol)
            if perp is not None:
                perp.downgraded_to_perp = True
                return perp
        return None

    async def _okx_spot(self, base: str) -> QuoteResult | None:
        return await self._okx_ticker(base, f"{base}-USDT", "spot", "okx_spot")

    async def _okx_perp(self, base: str) -> QuoteResult | None:
        return await self._okx_ticker(base, f"{base}-USDT-SWAP", "perp", "okx_perp")

    async def _okx_ticker(
        self,
        base: str,
        inst_id: str,
        contract_type: str,
        source: str,
    ) -> QuoteResult | None:
        url = "https://www.okx.com/api/v5/market/ticker"
        try:
            resp = await self.client.get(url, params={"instId": inst_id})
            if resp.status_code != 200:
                return None
            payload = resp.json()
            if payload.get("code") != "0":
                return None
            rows = payload.get("data") or []
            if not rows:
                return None
            row = rows[0]
            last = float(row["last"])
            open24 = float(row.get("open24h") or 0)
            change_pct = ((last - open24) / open24 * 100.0) if open24 > 0 else 0.0
            return QuoteResult(
                symbol=base,
                display_name=base,
                price=last,
                change_pct=change_pct,
                currency="$",
                market="crypto",
                contract_type=contract_type,
                source=source,
            )
        except Exception:
            return None

    async def _binance_vision_spot(self, base: str) -> QuoteResult | None:
        url = "https://data-api.binance.vision/api/v3/ticker/24hr"
        try:
            resp = await self.client.get(url, params={"symbol": f"{base}USDT"})
            if resp.status_code != 200:
                return None
            data = resp.json()
            if "lastPrice" not in data:
                return None
            return QuoteResult(
                symbol=base,
                display_name=base,
                price=float(data["lastPrice"]),
                change_pct=float(data.get("priceChangePercent", 0.0)),
                currency="$",
                market="crypto",
                contract_type="spot",
                source="binance_vision_spot",
            )
        except Exception:
            return None

    async def _binance_futures_perp(self, base: str) -> QuoteResult | None:
        url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        try:
            resp = await self.client.get(url, params={"symbol": f"{base}USDT"})
            if resp.status_code != 200:
                return None
            data = resp.json()
            if "lastPrice" not in data:
                return None
            return QuoteResult(
                symbol=base,
                display_name=base,
                price=float(data["lastPrice"]),
                change_pct=float(data.get("priceChangePercent", 0.0)),
                currency="$",
                market="crypto",
                contract_type="perp",
                source="binance_futures",
            )
        except Exception:
            return None

    async def _gate_spot(self, base: str) -> QuoteResult | None:
        url = "https://api.gateio.ws/api/v4/spot/tickers"
        try:
            resp = await self.client.get(url, params={"currency_pair": f"{base}_USDT"})
            if resp.status_code != 200:
                return None
            rows = resp.json()
            if not isinstance(rows, list) or not rows:
                return None
            row = rows[0]
            return QuoteResult(
                symbol=base,
                display_name=base,
                price=float(row["last"]),
                change_pct=float(row.get("change_percentage", 0.0)),
                currency="$",
                market="crypto",
                contract_type="spot",
                source="gate_spot",
            )
        except Exception:
            return None

    async def _gate_perp(self, base: str) -> QuoteResult | None:
        url = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
        try:
            resp = await self.client.get(url, params={"contract": f"{base}_USDT"})
            if resp.status_code != 200:
                return None
            rows = resp.json()
            if not isinstance(rows, list) or not rows:
                return None
            row = rows[0]
            return QuoteResult(
                symbol=base,
                display_name=base,
                price=float(row["last"]),
                change_pct=float(row.get("change_percentage", 0.0)),
                currency="$",
                market="crypto",
                contract_type="perp",
                source="gate_perp",
            )
        except Exception:
            return None
