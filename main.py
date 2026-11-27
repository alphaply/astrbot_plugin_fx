from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .formatter import format_label, format_quote_list
from .llm_resolver import LLMTargetResolver
from .models import QuoteResult, WatchlistItem
from .providers import CryptoProvider, EquityProvider, MarketRouter
from .storage import WatchlistStorage

_ADD_VERBS = {"添加", "加入", "新增", "add", "+"}
_REMOVE_VERBS = {"移除", "删除", "去掉", "del", "rm", "remove", "-"}
_HELP_VERBS = {"帮助", "help", "?", "？"}
_STANDALONE_MODIFIERS = {"合约", "永续", "永续合约", "perp", "swap"}


def split_target_tokens(raw_args: str) -> list[str]:
    """Split multi-target arguments, merging standalone modifiers like '合约 BTC'."""
    parts = [p for p in re.split(r"[\s,，、]+", raw_args.strip()) if p]
    tokens: list[str] = []
    i = 0
    while i < len(parts):
        cur = parts[i]
        if cur.lower() in _STANDALONE_MODIFIERS and i + 1 < len(parts):
            tokens.append(f"{parts[i + 1]}合约")
            i += 2
            continue
        if i + 1 < len(parts) and parts[i + 1].lower() in _STANDALONE_MODIFIERS:
            tokens.append(f"{cur}合约")
            i += 2
            continue
        tokens.append(cur)
        i += 1
    return tokens


class FxMarketPlugin(Star):
    """AstrBot multi-market quote and watchlist plugin."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.data_dir: Path = StarTools.get_data_dir()

        default_watchlist = self.config.get("default_watchlist") or [
            "BTC",
            "标普500",
            "纳斯达克",
            "沪深300",
            "恒生指数",
            "日经225",
            "富时100",
        ]
        self.storage = WatchlistStorage(
            data_dir=self.data_dir,
            default_symbols=default_watchlist,
        )

        timeout = float(self.config.get("request_timeout") or 10)
        proxy = str(self.config.get("proxy") or "").strip() or None
        client_kwargs: dict = {
            "timeout": httpx.Timeout(timeout),
            "follow_redirects": True,
            "headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                )
            },
        }
        if proxy:
            client_kwargs["proxy"] = proxy

        self.http_client = httpx.AsyncClient(**client_kwargs)
        self.crypto_provider = CryptoProvider(self.http_client)
        self.equity_provider = EquityProvider(self.http_client)
        self.router = MarketRouter(self.crypto_provider, self.equity_provider)

    async def terminate(self) -> None:
        await self.http_client.aclose()

    def _get_keyword(self) -> str:
        kw = str(self.config.get("command_keyword") or "fx").strip()
        return kw.lstrip("/") or "fx"

    def _get_color_style(self) -> str:
        style = str(self.config.get("color_style") or "cn").strip().lower()
        return style if style in ("cn", "us") else "cn"

    def _get_show_source(self) -> bool:
        return bool(self.config.get("show_source", True))

    def _get_output_format(self) -> str:
        fmt = str(self.config.get("output_format") or "plain").strip().lower()
        return fmt if fmt in ("plain", "markdown_table", "markdown_list") else "plain"

    def _get_icon_style(self) -> str:
        icon = str(self.config.get("change_icon_style") or "circle").strip().lower()
        return icon if icon in ("circle", "square", "arrow", "trend", "none") else "circle"

    def _get_max_watchlist_size(self) -> int:
        try:
            val = int(self.config.get("max_watchlist_size", 20))
            return max(0, val)
        except (TypeError, ValueError):
            return 20

    def _get_scope_key(self, event: AstrMessageEvent) -> str:
        scope = str(self.config.get("watchlist_scope") or "user").strip().lower()
        if scope == "global":
            return "global"
        if scope == "session":
            return f"session:{event.unified_msg_origin}"
        sender_id = event.get_sender_id() or event.unified_msg_origin
        return f"user:{sender_id}"

    def _get_llm_resolver(self) -> LLMTargetResolver | None:
        enabled = bool(self.config.get("enable_llm_fallback", True))
        if not enabled:
            return None
        provider_id = str(self.config.get("llm_provider_id") or "").strip()
        if not provider_id:
            return None
        return LLMTargetResolver(self.context, provider_id)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if not text:
            return

        keyword = self._get_keyword()
        pattern = rf"^/?{re.escape(keyword)}(?:\s+(.*))?$"
        match = re.match(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return

        event.stop_event()
        args = (match.group(1) or "").strip()

        if not args:
            reply = await self._handle_watchlist(event)
            yield event.plain_result(reply)
            return

        parts = args.split(maxsplit=1)
        subcmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if subcmd in _HELP_VERBS:
            yield event.plain_result(self._build_help(keyword))
            return

        if subcmd in _ADD_VERBS:
            if not rest:
                yield event.plain_result(f"用法：{keyword} 添加 BTC ETH SOL（支持 合约 如 BTC合约）")
                return
            reply = await self._handle_add(event, rest)
            yield event.plain_result(reply)
            return

        if subcmd in _REMOVE_VERBS:
            if not rest:
                yield event.plain_result(f"用法：{keyword} 移除 BTC ETH SOL")
                return
            reply = await self._handle_remove(event, rest)
            yield event.plain_result(reply)
            return

        reply = await self._handle_query(args)
        yield event.plain_result(reply)

    async def _handle_watchlist(self, event: AstrMessageEvent) -> str:
        scope_key = self._get_scope_key(event)
        items = self.storage.get_items(scope_key)
        if not items:
            kw = self._get_keyword()
            return f"自选清单为空，发送 `{kw} 添加 BTC ETH` 添加标的。"

        pairs = await self.router.query_many_items(items)
        quotes: list[QuoteResult] = []
        errors: list[str] = []
        for item, quote in pairs:
            if quote is not None:
                quotes.append(quote)
            else:
                errors.append(format_label(item.display_name, item.market, item.contract_type))

        if not quotes:
            return "暂未获取到自选行情数据，请稍后再试或检查网络/代理配置。"
        return format_quote_list(
            quotes,
            color_style=self._get_color_style(),
            errors=errors or None,
            show_source=self._get_show_source(),
            output_format=self._get_output_format(),
            title="自选行情",
            icon_style=self._get_icon_style(),
        )

    async def _handle_add(self, event: AstrMessageEvent, raw_targets: str) -> str:
        tokens = split_target_tokens(raw_targets)
        if not tokens:
            return "未识别到需要添加的标的。"

        llm_resolver = self._get_llm_resolver()
        results = await asyncio.gather(
            *(self.router.query_multi(tok, llm_resolver=llm_resolver) for tok in tokens),
            return_exceptions=True,
        )

        valid_items: list[WatchlistItem] = []
        downgraded: list[str] = []
        failed: list[str] = []

        for tok, res in zip(tokens, results):
            if isinstance(res, list) and res:
                for q in res:
                    valid_items.append(q.to_watchlist_item())
                    if q.downgraded_to_perp:
                        downgraded.append(q.display_name)
            else:
                if isinstance(res, Exception):
                    logger.warning(f"[fx] query failed for {tok}: {res}")
                failed.append(tok)

        # If splitting failed (e.g. multi-word phrase), try whole phrase with LLM
        if not valid_items and len(tokens) > 1:
            whole_quotes = await self.router.query_multi(raw_targets.strip(), llm_resolver=llm_resolver)
            if whole_quotes:
                valid_items = [q.to_watchlist_item() for q in whole_quotes]
                downgraded = [q.display_name for q in whole_quotes if q.downgraded_to_perp]
                failed = []

        scope_key = self._get_scope_key(event)
        max_size = self._get_max_watchlist_size()
        added: list[WatchlistItem] = []
        duplicates: list[WatchlistItem] = []
        rejected_limit: list[WatchlistItem] = []
        if valid_items:
            added, duplicates, rejected_limit = self.storage.add_items(
                scope_key,
                valid_items,
                max_size=max_size,
            )

        lines: list[str] = []
        if added:
            added_labels = [format_label(i.display_name, i.market, i.contract_type) for i in added]
            lines.append(f"✅ 已添加自选: {', '.join(added_labels)}")
        if downgraded:
            lines.append(f"ℹ️ 无现货，已降级为合约: {', '.join(downgraded)}")
        if duplicates:
            dup_labels = [format_label(i.display_name, i.market, i.contract_type) for i in duplicates]
            lines.append(f"• 已在自选中: {', '.join(dup_labels)}")
        if rejected_limit:
            rej_labels = [format_label(i.display_name, i.market, i.contract_type) for i in rejected_limit]
            lines.append(f"⚠️ 已达自选上限（{max_size}个），未添加: {', '.join(rej_labels)}")
        if failed:
            lines.append(f"⚠️ 未找到标的: {', '.join(failed)}")
        return "\n".join(lines) if lines else "未添加任何标的。"

    async def _handle_remove(self, event: AstrMessageEvent, raw_targets: str) -> str:
        tokens = split_target_tokens(raw_targets)
        if not tokens:
            return "未识别到需要移除的标的。"

        scope_key = self._get_scope_key(event)
        removed, not_found = self.storage.remove_items(scope_key, tokens)

        lines: list[str] = []
        if removed:
            removed_labels = [format_label(i.display_name, i.market, i.contract_type) for i in removed]
            lines.append(f"🗑️ 已移除自选: {', '.join(removed_labels)}")
        if not_found:
            lines.append(f"⚠️ 自选中未找到: {', '.join(not_found)}")
        return "\n".join(lines) if lines else "自选清单无变化。"

    async def _handle_query(self, raw_args: str) -> str:
        llm_resolver = self._get_llm_resolver()
        tokens = split_target_tokens(raw_args)

        if len(tokens) > 1:
            results = await asyncio.gather(
                *(self.router.query_multi(tok, llm_resolver=llm_resolver) for tok in tokens),
                return_exceptions=True,
            )
            quotes: list[QuoteResult] = []
            errors: list[str] = []
            for tok, res in zip(tokens, results):
                if isinstance(res, list) and res:
                    quotes.extend(res)
                else:
                    errors.append(tok)
            if quotes:
                return format_quote_list(
                    quotes,
                    color_style=self._get_color_style(),
                    errors=errors or None,
                    show_source=self._get_show_source(),
                    output_format=self._get_output_format(),
                    title="实时行情",
                    icon_style=self._get_icon_style(),
                )
            whole_quotes = await self.router.query_multi(raw_args.strip(), llm_resolver=llm_resolver)
            if whole_quotes:
                return format_quote_list(
                    whole_quotes,
                    color_style=self._get_color_style(),
                    show_source=self._get_show_source(),
                    output_format=self._get_output_format(),
                    title="实时行情",
                    icon_style=self._get_icon_style(),
                )
            return f"未查到标的「{raw_args.strip()}」的行情信息。"

        quotes = await self.router.query_multi(
            tokens[0] if tokens else raw_args,
            llm_resolver=llm_resolver,
        )
        if not quotes:
            return f"未查到标的「{raw_args.strip()}」的行情信息。"
        return format_quote_list(
            quotes,
            color_style=self._get_color_style(),
            show_source=self._get_show_source(),
            output_format=self._get_output_format(),
            title="实时行情",
            icon_style=self._get_icon_style(),
        )

    def _build_help(self, kw: str) -> str:
        if self._get_output_format() != "plain":
            return (
                f"### 📈 行情助手指令说明（前缀：`{kw}`）\n\n"
                "| 指令 | 说明 | 示例 |\n"
                "| :--- | :--- | :--- |\n"
                f"| `{kw}` | 查看当前自选清单 24h 行情 | `{kw}` |\n"
                f"| `{kw} <标的>` | 查询加密/A股/港股/美股/指数 | `{kw} btc` / `{kw} btc合约` / `{kw} 标普500` / `{kw} aapl` |\n"
                f"| `{kw} 添加 <标的...>` | 批量加入自选（默认现货，无现货自动降级合约） | `{kw} 添加 BTC ETH` |\n"
                f"| `{kw} 移除 <标的...>` | 批量移出自选 | `{kw} 移除 BTC` |\n"
                f"| `{kw} 帮助` | 查看帮助说明（支持 `{kw} help`） | `{kw} 帮助` |\n\n"
                f"> 💡 AI 辅助：机械接口未命中且已选 LLM Provider 时，支持「`{kw} 数字黄金`」「`{kw} 美股七巨头`」等模糊/合集识别"
            )
        return (
            f"📈 行情助手指令说明（当前前缀：{kw}）\n"
            f"• {kw} — 查看当前自选清单 24h 行情\n"
            f"• {kw} <标的> — 查询加密/A股/港股/美股/指数\n"
            f"  示例：{kw} btc / {kw} btc合约 / {kw} 标普500 / {kw} 上证指数 / {kw} aapl\n"
            f"• {kw} 添加 <标的1> <标的2>... — 批量加入自选（默认现货，无现货自动降级合约）\n"
            f"• {kw} 移除 <标的1> <标的2>... — 批量移出自选\n"
            f"• {kw} 帮助 — 查看本帮助说明（支持 {kw} help）\n"
            f"• AI 辅助：机械接口未命中且已选 LLM Provider 时，支持「{kw} 数字黄金」「{kw} 美股七巨头」等模糊/合集识别"
        )
