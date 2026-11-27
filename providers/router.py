from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from ..models import QuoteResult, WatchlistItem
from .crypto import CryptoProvider, normalize_crypto_symbol
from .equity import EquityProvider

if TYPE_CHECKING:
    from ..llm_resolver import LLMTargetResolver


# Common aliases mapped directly to deterministic symbols or EastMoney secids
BUILTIN_ALIASES: dict[str, tuple[str, str, str]] = {
    # Crypto aliases: (query_target, market, contract_type)
    "大饼": ("BTC", "crypto", "spot"),
    "比特币": ("BTC", "crypto", "spot"),
    "二饼": ("ETH", "crypto", "spot"),
    "以太坊": ("ETH", "crypto", "spot"),
    "以太": ("ETH", "crypto", "spot"),
    "索拉纳": ("SOL", "crypto", "spot"),
    "币安币": ("BNB", "crypto", "spot"),
    "泰达金": ("XAUT", "crypto", "spot"),
    "黄金": ("XAUT", "crypto", "spot"),
    "现货黄金": ("XAUT", "crypto", "spot"),
    "狗狗币": ("DOGE", "crypto", "spot"),
    "瑞波币": ("XRP", "crypto", "spot"),
    # Equity / Index aliases: (secid, market, display_name)
    "大a": ("1.000001", "index", "上证指数"),
    "沪指": ("1.000001", "index", "上证指数"),
    "上证": ("1.000001", "index", "上证指数"),
    "上证指数": ("1.000001", "index", "上证指数"),
    "深成指": ("0.399001", "index", "深证成指"),
    "深证成指": ("0.399001", "index", "深证成指"),
    "创业板": ("0.399006", "index", "创业板指"),
    "创业板指": ("0.399006", "index", "创业板指"),
    "沪深300": ("1.000300", "index", "沪深300"),
    "csi300": ("1.000300", "index", "沪深300"),
    "科创50": ("1.000688", "index", "科创50"),
    "恒指": ("100.HSI", "index", "恒生指数"),
    "恒生指数": ("100.HSI", "index", "恒生指数"),
    "hsi": ("100.HSI", "index", "恒生指数"),
    "纳指": ("100.NDX", "index", "纳斯达克"),
    "纳斯达克": ("100.NDX", "index", "纳斯达克"),
    "纳斯达克100": ("100.NDX", "index", "纳斯达克"),
    "ndx": ("100.NDX", "index", "纳斯达克"),
    "标普": ("100.SPX", "index", "标普500"),
    "标普500": ("100.SPX", "index", "标普500"),
    "spx": ("100.SPX", "index", "标普500"),
    "日经": ("100.N225", "index", "日经225"),
    "日经225": ("100.N225", "index", "日经225"),
    "n225": ("100.N225", "index", "日经225"),
    "富时": ("100.FTSE", "index", "英国富时100"),
    "富士": ("100.FTSE", "index", "英国富时100"),
    "富时100": ("100.FTSE", "index", "英国富时100"),
    "英国富时": ("100.FTSE", "index", "英国富时100"),
    "英国富时100": ("100.FTSE", "index", "英国富时100"),
    "ftse": ("100.FTSE", "index", "英国富时100"),
    "富时a50": ("100.XIN9", "index", "富时中国A50"),
    "富时中国a50": ("100.XIN9", "index", "富时中国A50"),
    "a50": ("100.XIN9", "index", "富时中国A50"),
}

KNOWN_CRYPTO_SYMBOLS = {
    "BTC", "ETH", "SOL", "BNB", "XAUT", "PAXG", "XRP", "DOGE", "ADA", "AVAX",
    "DOT", "LINK", "UNI", "LTC", "BCH", "SUI", "TON", "TRX", "PEPE", "WIF",
    "ARB", "OP", "APT", "NEAR", "ATOM", "FIL", "OKB", "AAVE", "HYPE", "TRUMP",
    "SHIB", "ETC", "XLM", "HBAR", "INJ", "TIA", "SEI", "JUP", "ORDI", "SATS",
    "WLD", "FET", "RENDER", "RNDR", "TAO", "ENA", "ONDO", "BONK", "FLOKI",
}

KNOWN_US_TICKERS = {
    "AAPL", "NVDA", "TSLA", "MSFT", "AMZN", "GOOG", "GOOGL", "META", "AMD",
    "INTC", "NFLX", "BABA", "PDD", "JD", "BIDU", "NIO", "XPEV", "LI",
    "COIN", "MSTR", "SPY", "QQQ", "VOO", "BRK.B", "TSM", "ASML", "AVGO",
    "PLTR", "HOOD", "ARM", "ORCL", "CRM", "UBER", "DIS",
}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_CN_HK_CODE_RE = re.compile(r"^(?:sh|sz|hk)?\d{5,6}$", re.IGNORECASE)
_PERP_TOKENS = (
    "-perp",
    "_perp",
    ":perp",
    " perp",
    "-swap",
    "_swap",
    ":swap",
    " swap",
    "永续合约",
    "永续",
    "合约",
)


def parse_contract_intent(raw: str) -> tuple[str, bool]:
    """Extract clean symbol/query and whether the user explicitly asked for futures/perp."""
    text = raw.strip()
    lower = text.lower()
    is_perp = False

    for token in _PERP_TOKENS:
        if lower.endswith(token) and len(text) > len(token):
            is_perp = True
            text = text[: len(text) - len(token)].strip(" -_:")
            break
        if lower.startswith(token) and len(text) > len(token):
            is_perp = True
            text = text[len(token) :].strip(" -_:")
            break

    return text, is_perp


class MarketRouter:
    """Routes user queries across crypto, equity/index, and optional LLM fallback."""

    def __init__(self, crypto: CryptoProvider, equity: EquityProvider) -> None:
        self.crypto = crypto
        self.equity = equity

    async def query(
        self,
        raw_query: str,
        llm_resolver: LLMTargetResolver | None = None,
    ) -> QuoteResult | None:
        results = await self.query_multi(raw_query, llm_resolver=llm_resolver)
        return results[0] if results else None

    async def query_multi(
        self,
        raw_query: str,
        llm_resolver: LLMTargetResolver | None = None,
    ) -> list[QuoteResult]:
        q = raw_query.strip()
        if not q:
            return []

        clean_q, is_perp = parse_contract_intent(q)
        if not clean_q:
            return []

        # 1. Mechanical lookup (aliases + heuristic market routing)
        result = await self._mechanical_lookup(clean_q, is_perp=is_perp)
        if result is not None:
            return [result]

        # 2. Optional LLM fallback when mechanical channels fail (supports multi-target baskets)
        if llm_resolver is not None:
            targets = await llm_resolver.resolve(q)
            if targets:
                fetched = await asyncio.gather(
                    *(
                        self._fetch_by_target(
                            t["symbol"],
                            market=t["market"],
                            contract_type="perp" if is_perp else t["contract_type"],
                        )
                        for t in targets
                    ),
                    return_exceptions=True,
                )
                quotes: list[QuoteResult] = []
                seen: set[tuple[str, str, str]] = set()
                for item in fetched:
                    if not isinstance(item, QuoteResult):
                        continue
                    key = (item.market, item.symbol.upper(), item.contract_type)
                    if key in seen:
                        continue
                    seen.add(key)
                    item.ai_resolved_from = q
                    quotes.append(item)
                return quotes

        return []

    async def _mechanical_lookup(self, clean_q: str, is_perp: bool) -> QuoteResult | None:
        lower = clean_q.lower()

        # Check explicit built-in aliases first
        alias = BUILTIN_ALIASES.get(lower)
        if alias is not None:
            target, market, third = alias
            if market == "crypto":
                contract = "perp" if is_perp else third
                return await self.crypto.get_quote(target, contract_type=contract, allow_downgrade=True)
            return await self.equity.get_quote_by_secid(
                secid=target,
                market_hint=market,
                display_name_hint=third,
            )

        # Explicit contract intent -> crypto futures directly
        if is_perp:
            return await self.crypto.get_quote(clean_q, contract_type="perp", allow_downgrade=False)

        # CJK characters or 5-6 digit stock code -> equity / index first
        if _CJK_RE.search(clean_q) or _CN_HK_CODE_RE.fullmatch(clean_q):
            return await self.equity.get_quote(clean_q)

        upper = normalize_crypto_symbol(clean_q)

        # Known US stock tickers (avoid confusion if any shares name)
        if clean_q.upper() in KNOWN_US_TICKERS:
            eq = await self.equity.get_quote(clean_q.upper())
            if eq is not None:
                return eq
            return await self.crypto.get_quote(upper, contract_type="spot", allow_downgrade=True)

        # Known crypto symbols
        if upper in KNOWN_CRYPTO_SYMBOLS:
            return await self.crypto.get_quote(upper, contract_type="spot", allow_downgrade=True)

        # Unknown ASCII symbol (e.g. long-tail token or less-known US ticker):
        # Try crypto spot -> equity -> crypto perp (spot-missing downgrade)
        spot = await self.crypto.get_spot(upper)
        if spot is not None:
            return spot

        eq = await self.equity.get_quote(clean_q)
        if eq is not None:
            return eq

        perp = await self.crypto.get_perp(upper)
        if perp is not None:
            perp.downgraded_to_perp = True
            return perp

        return None

    async def _fetch_by_target(
        self,
        symbol: str,
        market: str,
        contract_type: str = "spot",
    ) -> QuoteResult | None:
        if market == "crypto":
            return await self.crypto.get_quote(
                symbol,
                contract_type="perp" if contract_type == "perp" else "spot",
                allow_downgrade=True,
            )
        if "." in symbol and symbol.split(".", 1)[0].isdigit():
            return await self.equity.get_quote_by_secid(symbol, market_hint=market)
        return await self.equity.get_quote(symbol)

    async def query_watchlist_item(self, item: WatchlistItem) -> QuoteResult | None:
        if item.market == "crypto":
            return await self.crypto.get_quote(
                item.symbol,
                contract_type=item.normalized_contract_type(),
                allow_downgrade=True,
            )
        if item.quote_id:
            quote = await self.equity.get_quote_by_secid(
                secid=item.quote_id,
                market_hint=item.market,
                display_name_hint=item.display_name,
                symbol_hint=item.symbol,
            )
            if quote is not None:
                return quote
        return await self.equity.get_quote(item.symbol or item.display_name)

    async def query_many_items(
        self,
        items: list[WatchlistItem],
    ) -> list[tuple[WatchlistItem, QuoteResult | None]]:
        results = await asyncio.gather(
            *(self.query_watchlist_item(item) for item in items),
            return_exceptions=True,
        )
        output: list[tuple[WatchlistItem, QuoteResult | None]] = []
        for item, res in zip(items, results):
            if isinstance(res, QuoteResult):
                output.append((item, res))
            else:
                output.append((item, None))
        return output
