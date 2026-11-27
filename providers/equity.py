from __future__ import annotations

import re

import httpx

from ..models import QuoteResult

_MARKET_CURRENCY = {
    "cn": "¥",
    "hk": "HK$",
    "us": "$",
    "index": "",
    "eu": "€",
}

_PRIMARY_CLASSIFIES = {"AStock", "UsStock", "HK", "Index", "UniversalIndex"}
_DERIVATIVE_KEYWORDS = ("etf", "期权", "做多", "做空", "收益策略", "购", "沽", "牛", "熊")
_SHORT_NAMES = {
    "英国富时100": "富时100",
}


def _classify_eastmoney(item: dict) -> tuple[str, str]:
    """Map EastMoney suggest record to (market, currency)."""
    classify = str(item.get("Classify") or "")
    sec_type = str(item.get("SecurityTypeName") or "")
    mkt_num = str(item.get("MktNum") or "")

    if classify in ("Index", "UniversalIndex") or "指数" in sec_type:
        return "index", ""
    if classify == "HK" or mkt_num == "116":
        return "hk", "HK$"
    if classify == "UsStock" or mkt_num in ("105", "106", "107"):
        return "us", "$"
    if classify == "XETR" or mkt_num == "185" or "德股" in sec_type:
        return "eu", "€"
    if classify in ("AStock", "Fund", "Bond") or mkt_num in ("0", "1"):
        return "cn", "¥"
    if mkt_num == "100":
        return "index", ""
    return "cn", "¥"


def _score_suggest_candidate(row: dict, query: str) -> float:
    """Rank EastMoney suggest results, preferring primary equity/index listings over XETR/derivatives."""
    classify = str(row.get("Classify") or "")
    code = str(row.get("Code") or "").strip()
    name = str(row.get("Name") or "").strip()

    q_lower = query.lower()
    code_lower = code.lower()
    name_lower = name.lower()

    score = 0.0
    if classify in _PRIMARY_CLASSIFIES:
        score += 100.0
    elif classify == "Fund":
        score += 50.0
    else:
        score -= 100.0

    if not any(k in q_lower for k in ("etf", "购", "沽")):
        if any(k in name_lower for k in _DERIVATIVE_KEYWORDS):
            score -= 40.0

    if code_lower == q_lower:
        score += 80.0
    if name_lower == q_lower:
        score += 60.0
    elif name_lower.startswith(q_lower):
        score += 40.0

    score -= len(name) * 0.5
    return score


def _secid_to_tx_sina(secid: str, market: str) -> tuple[str | None, str | None]:
    """Convert EastMoney secid (e.g. 1.000001, 116.00700, 105.AAPL) to Tencent & Sina codes."""
    if "." not in secid:
        return None, None
    mkt_num, code = secid.split(".", 1)
    if mkt_num == "1":
        tx = f"sh{code}"
        return tx, tx
    if mkt_num == "0":
        tx = f"sz{code}"
        return tx, tx
    if mkt_num == "116":
        tx = f"hk{code}"
        return tx, tx
    if mkt_num in ("105", "106", "107") or market == "us":
        return f"us{code.upper()}", f"gb_{code.lower()}"
    if mkt_num == "100" and code.upper() == "HSI":
        return "hkHSI", "hkHSI"
    return None, None


def _safe_float(val: object, default: float | None = None) -> float | None:
    if val is None or val == "-" or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


class EquityProvider:
    """Free public quote provider for A-shares, HK stocks, US stocks, and global indices."""

    SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"
    QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
    TENCENT_URL = "https://qt.gtimg.cn/q={code}"
    SINA_URL = "https://hq.sinajs.cn/list=s_{code}"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def search_eastmoney(self, query: str) -> dict | None:
        q = query.strip()
        if not q:
            return None
        try:
            resp = await self.client.get(
                self.SUGGEST_URL,
                params={"input": q, "type": 14, "count": 10},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            rows = (data.get("QuotationCodeTable") or {}).get("Data") or []
            if not rows:
                return None

            return max(rows, key=lambda row: _score_suggest_candidate(row, q))
        except Exception:
            return None

    async def get_quote_by_secid(
        self,
        secid: str,
        market_hint: str = "",
        display_name_hint: str = "",
        symbol_hint: str = "",
    ) -> QuoteResult | None:
        # 1. Try EastMoney primary endpoint
        em_quote = await self._eastmoney_quote(secid, market_hint, display_name_hint, symbol_hint)
        if em_quote is not None:
            return em_quote

        # 2. Fallback to Tencent / Sina if mapped
        tx_code, sina_code = _secid_to_tx_sina(secid, market_hint)
        if tx_code:
            tx_quote = await self.get_quote_tencent(tx_code, market_hint, display_name_hint, secid)
            if tx_quote is not None:
                return tx_quote
        if sina_code and market_hint in ("cn", "index"):
            sina_quote = await self.get_quote_sina(sina_code, market_hint, display_name_hint, secid)
            if sina_quote is not None:
                return sina_quote
        return None

    async def _eastmoney_quote(
        self,
        secid: str,
        market_hint: str = "",
        display_name_hint: str = "",
        symbol_hint: str = "",
    ) -> QuoteResult | None:
        try:
            resp = await self.client.get(
                self.QUOTE_URL,
                params={
                    "secid": secid,
                    "fields": "f43,f57,f58,f59,f60,f169,f170",
                },
            )
            if resp.status_code != 200:
                return None
            payload = resp.json()
            data = payload.get("data")
            if not isinstance(data, dict):
                return None

            decimals = int(_safe_float(data.get("f59"), 2) or 2)
            scale = 10 ** decimals

            raw_price = _safe_float(data.get("f43"))
            if raw_price is None or raw_price == 0:
                raw_price = _safe_float(data.get("f60"))
            if raw_price is None:
                return None
            price = raw_price / scale

            raw_pct = _safe_float(data.get("f170"), 0.0) or 0.0
            change_pct = raw_pct / 100.0

            code = str(data.get("f57") or symbol_hint or secid.split(".")[-1]).strip()
            name = str(data.get("f58") or display_name_hint or code).strip()

            market = market_hint
            if not market:
                mkt_prefix = secid.split(".", 1)[0] if "." in secid else ""
                if mkt_prefix == "116":
                    market = "hk"
                elif mkt_prefix in ("105", "106", "107"):
                    market = "us"
                elif mkt_prefix == "100":
                    market = "index"
                else:
                    market = "index" if "指数" in name else "cn"

            currency = _MARKET_CURRENCY.get(market, "")
            # For US stocks, ticker (AAPL) is more compact than Chinese name when user queries ticker
            display_name = code if (market == "us" and code.isascii() and code.isalpha()) else name
            display_name = _SHORT_NAMES.get(display_name, display_name)

            return QuoteResult(
                symbol=code,
                display_name=display_name,
                price=price,
                change_pct=change_pct,
                currency=currency,
                market=market,
                contract_type="spot",
                quote_id=secid,
                source="eastmoney",
            )
        except Exception:
            return None

    async def get_quote_tencent(
        self,
        tx_code: str,
        market_hint: str = "",
        display_name_hint: str = "",
        quote_id: str = "",
    ) -> QuoteResult | None:
        try:
            resp = await self.client.get(self.TENCENT_URL.format(code=tx_code))
            if resp.status_code != 200:
                return None
            text = resp.content.decode("gbk", errors="replace").strip()
            match = re.search(r'="([^"]+)"', text)
            if not match:
                return None
            parts = match.group(1).split("~")
            if len(parts) < 33:
                return None
            name = (parts[1] or display_name_hint or tx_code).strip()
            code = (parts[2] or tx_code).strip()
            price = _safe_float(parts[3])
            if price is None:
                return None
            change_pct = _safe_float(parts[32], 0.0) or 0.0

            market = market_hint
            if not market:
                if tx_code.startswith("hk"):
                    market = "index" if code == "HSI" else "hk"
                elif tx_code.startswith("us"):
                    market = "us"
                else:
                    market = "index" if "指数" in name else "cn"

            currency = _MARKET_CURRENCY.get(market, "")
            clean_code = code.split(".")[0] if "." in code else code
            display_name = clean_code if market == "us" else name

            return QuoteResult(
                symbol=clean_code,
                display_name=display_name,
                price=price,
                change_pct=change_pct,
                currency=currency,
                market=market,
                contract_type="spot",
                quote_id=quote_id,
                source="tencent",
            )
        except Exception:
            return None

    async def get_quote_sina(
        self,
        sina_code: str,
        market_hint: str = "cn",
        display_name_hint: str = "",
        quote_id: str = "",
    ) -> QuoteResult | None:
        try:
            resp = await self.client.get(
                self.SINA_URL.format(code=sina_code),
                headers={"Referer": "https://finance.sina.com.cn"},
            )
            if resp.status_code != 200:
                return None
            text = resp.content.decode("gbk", errors="replace").strip()
            match = re.search(r'="([^"]+)"', text)
            if not match:
                return None
            parts = match.group(1).split(",")
            if len(parts) < 4:
                return None
            name = (parts[0] or display_name_hint or sina_code).strip()
            price = _safe_float(parts[1])
            change_pct = _safe_float(parts[3], 0.0) or 0.0
            if price is None:
                return None
            market = "index" if "指数" in name else market_hint
            currency = _MARKET_CURRENCY.get(market, "¥")
            return QuoteResult(
                symbol=re.sub(r"^(sh|sz)", "", sina_code),
                display_name=name,
                price=price,
                change_pct=change_pct,
                currency=currency,
                market=market,
                contract_type="spot",
                quote_id=quote_id,
                source="sina",
            )
        except Exception:
            return None

    async def get_quote(self, query: str) -> QuoteResult | None:
        q = query.strip()
        if not q:
            return None

        # Direct prefixed code support like sh000001, sz399001, hk00700
        if re.fullmatch(r"(sh|sz|hk)\d{5,6}", q.lower()):
            tx_quote = await self.get_quote_tencent(q.lower())
            if tx_quote is not None:
                return tx_quote

        matched = await self.search_eastmoney(q)
        if not matched:
            return None

        secid = str(matched.get("QuoteID") or "").strip()
        if not secid:
            return None
        market, _ = _classify_eastmoney(matched)
        name = str(matched.get("Name") or q).strip()
        code = str(matched.get("Code") or q).strip()

        return await self.get_quote_by_secid(
            secid=secid,
            market_hint=market,
            display_name_hint=name,
            symbol_hint=code,
        )
