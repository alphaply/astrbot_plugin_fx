from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable

from .models import WatchlistItem
from .providers.router import BUILTIN_ALIASES, KNOWN_US_TICKERS

DEFAULT_SYMBOLS = ["BTC", "标普500", "纳斯达克", "沪深300", "恒生指数", "日经225", "富时100"]

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_CN_HK_CODE_RE = re.compile(r"^(?:sh|sz|hk)?\d{5,6}$", re.IGNORECASE)


def parse_default_entry(raw: str) -> WatchlistItem:
    """Convert a default watchlist entry string into a WatchlistItem."""
    text = raw.strip()
    lower = text.lower()
    contract_type = "spot"
    for suffix in ("-perp", ":perp", "永续合约", "永续", "合约"):
        if lower.endswith(suffix):
            contract_type = "perp"
            text = text[: len(text) - len(suffix)].strip()
            lower = text.lower()
            break

    alias = BUILTIN_ALIASES.get(lower)
    if alias is not None:
        target, market, third = alias
        if market == "crypto":
            return WatchlistItem(
                symbol=target,
                display_name=target,
                market="crypto",
                contract_type="perp" if contract_type == "perp" else third,
            )
        sec_code = target.split(".", 1)[-1]
        return WatchlistItem(
            symbol=sec_code,
            display_name=third,
            market=market,
            contract_type="spot",
            quote_id=target,
        )

    if _CJK_RE.search(text) or _CN_HK_CODE_RE.fullmatch(text):
        return WatchlistItem(
            symbol=text,
            display_name=text,
            market="cn",
            contract_type="spot",
        )

    upper = text.upper()
    if upper in KNOWN_US_TICKERS:
        return WatchlistItem(
            symbol=upper,
            display_name=upper,
            market="us",
            contract_type="spot",
        )

    return WatchlistItem(
        symbol=upper,
        display_name=upper,
        market="crypto",
        contract_type=contract_type,
    )


class WatchlistStorage:
    """JSON-backed watchlist persistence using atomic file replacement."""

    def __init__(
        self,
        data_dir: Path,
        default_symbols: Iterable[str] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.data_dir / "watchlists.json"
        symbols = [str(s).strip() for s in (default_symbols or DEFAULT_SYMBOLS) if str(s).strip()]
        self._default_symbols = symbols or list(DEFAULT_SYMBOLS)

    def _build_default_items(self) -> list[WatchlistItem]:
        return [parse_default_entry(sym) for sym in self._default_symbols]

    def _load_all(self) -> dict[str, list[dict]]:
        if not self.file_path.exists():
            return {}
        try:
            raw = self.file_path.read_text(encoding="utf-8")
            if not raw.strip():
                return {}
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            return {}
        return {}

    def _save_all(self, data: dict[str, list[dict]]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix="watchlists_",
            suffix=".tmp",
            dir=str(self.data_dir),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.file_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def get_items(self, scope_key: str) -> list[WatchlistItem]:
        all_data = self._load_all()
        if scope_key not in all_data:
            defaults = self._build_default_items()
            all_data[scope_key] = [item.to_dict() for item in defaults]
            self._save_all(all_data)
            return defaults
        raw_items = all_data.get(scope_key, [])
        items: list[WatchlistItem] = []
        for entry in raw_items:
            if isinstance(entry, dict) and entry.get("symbol"):
                items.append(WatchlistItem.from_dict(entry))
        return items

    def add_items(
        self,
        scope_key: str,
        new_items: list[WatchlistItem],
    ) -> tuple[list[WatchlistItem], list[WatchlistItem]]:
        all_data = self._load_all()
        if scope_key not in all_data:
            existing = self._build_default_items()
        else:
            existing = [
                WatchlistItem.from_dict(e)
                for e in all_data[scope_key]
                if isinstance(e, dict) and e.get("symbol")
            ]

        existing_keys = {item.unique_key() for item in existing}
        added: list[WatchlistItem] = []
        duplicates: list[WatchlistItem] = []

        for item in new_items:
            key = item.unique_key()
            if key in existing_keys:
                duplicates.append(item)
                continue
            existing.append(item)
            existing_keys.add(key)
            added.append(item)

        all_data[scope_key] = [item.to_dict() for item in existing]
        self._save_all(all_data)
        return added, duplicates

    def remove_items(
        self,
        scope_key: str,
        queries: list[str],
    ) -> tuple[list[WatchlistItem], list[str]]:
        all_data = self._load_all()
        if scope_key not in all_data:
            existing = self._build_default_items()
        else:
            existing = [
                WatchlistItem.from_dict(e)
                for e in all_data[scope_key]
                if isinstance(e, dict) and e.get("symbol")
            ]

        removed: list[WatchlistItem] = []
        not_found: list[str] = []

        for raw_q in queries:
            q = raw_q.strip()
            if not q:
                continue
            want_perp = False
            q_clean = q
            q_lower = q.lower()
            for token in ("-perp", ":perp", "_perp", "-swap", ":swap", "合约", "永续"):
                if q_lower.endswith(token):
                    want_perp = True
                    q_clean = q[: len(q) - len(token)].strip()
                    break
                if q_lower.startswith(token):
                    want_perp = True
                    q_clean = q[len(token) :].strip()
                    break

            q_clean_lower = q_clean.lower()
            matched_indices: list[int] = []
            for idx, item in enumerate(existing):
                sym_match = item.symbol.lower() == q_clean_lower
                name_match = item.display_name.lower() == q_clean_lower or item.display_name.lower() == q_lower
                if not (sym_match or name_match):
                    continue
                if want_perp and item.contract_type != "perp":
                    continue
                matched_indices.append(idx)

            if not matched_indices:
                not_found.append(q)
                continue

            # If user didn't explicitly ask for perp and both spot and perp exist, remove spot first or all matching
            if not want_perp and len(matched_indices) > 1:
                spot_only = [i for i in matched_indices if existing[i].contract_type == "spot"]
                if spot_only:
                    matched_indices = spot_only

            for idx in reversed(matched_indices):
                removed.append(existing.pop(idx))

        all_data[scope_key] = [item.to_dict() for item in existing]
        self._save_all(all_data)
        return removed, not_found
