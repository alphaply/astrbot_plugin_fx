from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class WatchlistItem:
    """Represents a single saved asset in a user's watchlist."""

    symbol: str
    display_name: str
    market: str = "crypto"  # crypto | cn | hk | us | index
    contract_type: str = "spot"  # spot | perp (crypto only)
    quote_id: str = ""  # Optional provider-specific ID (e.g. EastMoney secid 1.000001)

    def normalized_contract_type(self) -> str:
        if self.market != "crypto":
            return "spot"
        return "perp" if self.contract_type == "perp" else "spot"

    def unique_key(self) -> str:
        return f"{self.market}:{self.symbol.upper()}:{self.normalized_contract_type()}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["contract_type"] = self.normalized_contract_type()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WatchlistItem:
        market = str(data.get("market") or "crypto").lower()
        contract_type = str(data.get("contract_type") or "spot").lower()
        if market != "crypto":
            contract_type = "spot"
        elif contract_type not in ("spot", "perp"):
            contract_type = "spot"
        symbol = str(data.get("symbol") or "").strip()
        display_name = str(data.get("display_name") or symbol).strip()
        quote_id = str(data.get("quote_id") or "").strip()
        return cls(
            symbol=symbol,
            display_name=display_name,
            market=market,
            contract_type=contract_type,
            quote_id=quote_id,
        )


@dataclass
class QuoteResult:
    """Normalized market quote returned by providers."""

    symbol: str
    display_name: str
    price: float
    change_pct: float
    currency: str = "$"
    market: str = "crypto"
    contract_type: str = "spot"
    quote_id: str = ""
    source: str = ""
    ai_resolved_from: str = ""
    downgraded_to_perp: bool = False

    def to_watchlist_item(self) -> WatchlistItem:
        return WatchlistItem(
            symbol=self.symbol,
            display_name=self.display_name,
            market=self.market,
            contract_type=self.contract_type if self.market == "crypto" else "spot",
            quote_id=self.quote_id,
        )
