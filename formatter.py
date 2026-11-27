from __future__ import annotations

from .models import QuoteResult

_SOURCE_LABELS = {
    "okx_spot": "OKX",
    "okx_perp": "OKX",
    "binance_vision_spot": "Binance",
    "binance_futures": "Binance",
    "gate_spot": "Gate",
    "gate_perp": "Gate",
    "eastmoney": "东财",
    "tencent": "腾讯",
    "sina": "新浪",
}


def format_source_label(source: str) -> str:
    if not source:
        return ""
    return _SOURCE_LABELS.get(source, source)


def format_price(price: float, currency: str = "$") -> str:
    """Format market price with appropriate thousands separators and decimals."""
    abs_p = abs(price)
    if abs_p >= 1000:
        num_str = f"{price:,.2f}"
    elif abs_p >= 1:
        num_str = f"{price:,.2f}"
    elif abs_p == 0:
        num_str = "0.00"
    elif abs_p >= 0.01:
        num_str = f"{price:.4f}".rstrip("0").rstrip(".")
        if "." in num_str and len(num_str.split(".")[1]) < 2:
            num_str = f"{price:.2f}"
    else:
        # Micro-cap tokens (e.g. PEPE, SHIB)
        num_str = f"{price:.8f}".rstrip("0").rstrip(".")
    return f"{currency}{num_str}"


def format_change(change_pct: float, color_style: str = "cn") -> str:
    """Format 24h/daily percentage change with configurable color dot."""
    if change_pct > 0:
        dot = "🔴" if color_style == "cn" else "🟢"
        return f"{dot}+{change_pct:.2f}%"
    if change_pct < 0:
        dot = "🟢" if color_style == "cn" else "🔴"
        return f"{dot}{change_pct:.2f}%"
    return "⚪0.00%"


def format_label(display_name: str, market: str, contract_type: str) -> str:
    if market == "crypto" and contract_type == "perp":
        return f"{display_name}[合约]"
    return display_name


def format_quote_line(
    quote: QuoteResult,
    color_style: str = "cn",
    show_source: bool = True,
) -> str:
    label = format_label(quote.display_name, quote.market, quote.contract_type)
    price_str = format_price(quote.price, quote.currency)
    change_str = format_change(quote.change_pct, color_style=color_style)
    line = f"{label} {price_str} ({change_str})"
    if show_source and quote.source:
        src_label = format_source_label(quote.source)
        if src_label:
            line = f"{line} [{src_label}]"
    return line


def format_quote_list(
    quotes: list[QuoteResult],
    color_style: str = "cn",
    errors: list[str] | None = None,
    show_source: bool = True,
) -> str:
    lines = [
        format_quote_line(q, color_style=color_style, show_source=show_source)
        for q in quotes
    ]

    ai_groups: dict[str, list[str]] = {}
    downgrade_notes: list[str] = []
    for q in quotes:
        if q.ai_resolved_from:
            label = format_label(q.display_name, q.market, q.contract_type)
            ai_groups.setdefault(q.ai_resolved_from, []).append(label)
        if q.downgraded_to_perp:
            downgrade_notes.append(q.display_name)

    for orig, targets in ai_groups.items():
        if len(targets) == 1:
            lines.append(f"💡 AI识别: {orig} → {targets[0]}")
        else:
            lines.append(f"💡 AI识别「{orig}」→ {', '.join(targets)}")
    if downgrade_notes:
        lines.append(f"ℹ️ 无现货，已自动切换合约: {', '.join(downgrade_notes)}")
    if errors:
        lines.append(f"⚠️ 未获取到行情: {', '.join(errors)}")
    return "\n".join(lines)
