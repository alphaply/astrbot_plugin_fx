from __future__ import annotations

import json
import re
from typing import Any

_SYSTEM_PROMPT = """你是一个金融行情标的识别助手。用户会输入非标准名称、俗称、板块/概念合集或模糊金融描述。
请将其解析为一个或多个标准金融标的，并严格只返回一行合法 JSON（不要输出解释、不要使用 Markdown）：
{"targets": [{"symbol": "标准代码或名称", "market": "crypto|cn|hk|us|index", "contract_type": "spot|perp"}]}

规则：
1. 单个标的：
   - "数字黄金" -> {"targets":[{"symbol":"BTC","market":"crypto","contract_type":"spot"}]}
   - "水果公司" -> {"targets":[{"symbol":"AAPL","market":"us","contract_type":"spot"}]}
   - "宇宙行" -> {"targets":[{"symbol":"601398","market":"cn","contract_type":"spot"}]}
   - "鹅厂" -> {"targets":[{"symbol":"00700","market":"hk","contract_type":"spot"}]}
2. 多个/合集标的（如板块代表、知名组合，最多返回12个）：
   - "美股七巨头" -> {"targets":[{"symbol":"AAPL","market":"us","contract_type":"spot"},{"symbol":"MSFT","market":"us","contract_type":"spot"},{"symbol":"NVDA","market":"us","contract_type":"spot"},{"symbol":"GOOGL","market":"us","contract_type":"spot"},{"symbol":"AMZN","market":"us","contract_type":"spot"},{"symbol":"META","market":"us","contract_type":"spot"},{"symbol":"TSLA","market":"us","contract_type":"spot"}]}
   - "三大指数" -> {"targets":[{"symbol":"上证指数","market":"index","contract_type":"spot"},{"symbol":"深证成指","market":"index","contract_type":"spot"},{"symbol":"创业板指","market":"index","contract_type":"spot"}]}
3. 若用户提到合约或永续，对应标的 contract_type 为 "perp"，否则为 "spot"。
4. 如果完全无法判断为金融标的，返回 {"targets": []}。"""


def _extract_json_object(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _normalize_target(item: Any) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    symbol = item.get("symbol")
    if not symbol or not isinstance(symbol, str) or not symbol.strip():
        return None
    market = str(item.get("market") or "crypto").lower().strip()
    if market not in ("crypto", "cn", "hk", "us", "index"):
        market = "crypto"
    contract_type = str(item.get("contract_type") or "spot").lower().strip()
    if market != "crypto" or contract_type not in ("spot", "perp"):
        contract_type = "spot"
    return {
        "symbol": symbol.strip(),
        "market": market,
        "contract_type": contract_type,
    }


class LLMTargetResolver:
    """Resolves ambiguous or multi-target queries using a configured AstrBot LLM provider."""

    MAX_TARGETS = 12

    def __init__(self, context: Any, provider_id: str) -> None:
        self.context = context
        self.provider_id = provider_id.strip()

    async def resolve(self, query: str) -> list[dict[str, str]]:
        if not self.provider_id or not query.strip():
            return []

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.provider_id,
                prompt=f"请识别用户输入对应的金融标的：{query.strip()}",
                system_prompt=_SYSTEM_PROMPT,
                temperature=0.1,
                max_tokens=400,
            )
        except Exception:
            return []

        raw_text = ""
        if isinstance(resp, str):
            raw_text = resp
        elif resp is not None and hasattr(resp, "completion_text"):
            raw_text = str(resp.completion_text or "")

        if not raw_text.strip():
            return []

        data = _extract_json_object(raw_text)
        if not data:
            return []

        raw_list: list[Any]
        if isinstance(data.get("targets"), list):
            raw_list = data["targets"]
        elif "symbol" in data:
            raw_list = [data]
        else:
            return []

        targets: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for entry in raw_list:
            normalized = _normalize_target(entry)
            if not normalized:
                continue
            key = (
                normalized["market"],
                normalized["symbol"].upper(),
                normalized["contract_type"],
            )
            if key in seen:
                continue
            seen.add(key)
            targets.append(normalized)
            if len(targets) >= self.MAX_TARGETS:
                break

        return targets
