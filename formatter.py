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


def format_change(
    change_pct: float,
    color_style: str = "cn",
    icon_style: str = "circle",
) -> str:
    """Format 24h/daily percentage change with configurable color and icon style."""
    is_cn = color_style != "us"
    if icon_style == "square":
        up, down, flat = ("🟥", "🟩", "⬜") if is_cn else ("🟩", "🟥", "⬜")
    elif icon_style == "arrow":
        up, down, flat = "▲", "▼", "—"
    elif icon_style == "trend":
        up, down, flat = "📈", "📉", "➖"
    elif icon_style == "none":
        up, down, flat = "", "", ""
    else:
        up, down, flat = ("🔴", "🟢", "⚪") if is_cn else ("🟢", "🔴", "⚪")

    if change_pct > 0:
        return f"{up}+{change_pct:.2f}%"
    if change_pct < 0:
        return f"{down}{change_pct:.2f}%"
    return f"{flat}0.00%"


def format_label(display_name: str, market: str, contract_type: str) -> str:
    if market == "crypto" and contract_type == "perp":
        return f"{display_name}[合约]"
    return display_name


def format_quote_line(
    quote: QuoteResult,
    color_style: str = "cn",
    show_source: bool = True,
    icon_style: str = "circle",
) -> str:
    label = format_label(quote.display_name, quote.market, quote.contract_type)
    price_str = format_price(quote.price, quote.currency)
    change_str = format_change(
        quote.change_pct,
        color_style=color_style,
        icon_style=icon_style,
    )
    line = f"{label} {price_str} ({change_str})"
    if show_source and quote.source:
        src_label = format_source_label(quote.source)
        if src_label:
            line = f"{line} [{src_label}]"
    return line


def _escape_md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _format_source_footer(quotes: list[QuoteResult]) -> str:
    seen: list[str] = []
    for q in quotes:
        if q.source:
            label = format_source_label(q.source)
            if label and label not in seen:
                seen.append(label)
    if not seen:
        return ""
    return f"📡 来源: {' · '.join(seen)}"


def _collect_notes(
    quotes: list[QuoteResult],
    errors: list[str] | None,
    include_sources: bool = False,
) -> list[str]:
    ai_groups: dict[str, list[str]] = {}
    downgrade_notes: list[str] = []
    for q in quotes:
        if q.ai_resolved_from:
            label = format_label(q.display_name, q.market, q.contract_type)
            ai_groups.setdefault(q.ai_resolved_from, []).append(label)
        if q.downgraded_to_perp:
            downgrade_notes.append(q.display_name)

    notes: list[str] = []
    for orig, targets in ai_groups.items():
        if len(targets) == 1:
            notes.append(f"💡 AI识别: {orig} → {targets[0]}")
        else:
            notes.append(f"💡 AI识别「{orig}」→ {', '.join(targets)}")
    if downgrade_notes:
        notes.append(f"ℹ️ 无现货，已自动切换合约: {', '.join(downgrade_notes)}")
    if include_sources:
        src_footer = _format_source_footer(quotes)
        if src_footer:
            notes.append(src_footer)
    if errors:
        notes.append(f"⚠️ 未获取到行情: {', '.join(errors)}")
    return notes


def format_quote_list(
    quotes: list[QuoteResult],
    color_style: str = "cn",
    errors: list[str] | None = None,
    show_source: bool = True,
    output_format: str = "plain",
    title: str = "",
    icon_style: str = "circle",
) -> str:
    if output_format == "markdown_table":
        notes = _collect_notes(quotes, errors, include_sources=show_source)
        heading = f"### 📈 {title.strip() or '实时行情'}"
        rows = [
            heading,
            "",
            "| 标的 | 价格 | 涨跌 |",
            "| :--- | ---: | ---: |",
        ]
        for q in quotes:
            label = _escape_md_cell(format_label(q.display_name, q.market, q.contract_type))
            price_str = _escape_md_cell(format_price(q.price, q.currency))
            change_str = _escape_md_cell(
                format_change(q.change_pct, color_style=color_style, icon_style=icon_style)
            )
            rows.append(f"| **{label}** | {price_str} | {change_str} |")
        if notes:
            rows.append("")
            rows.extend(f"> {note}" for note in notes)
        return "\n".join(rows)

    if output_format == "markdown_list":
        notes = _collect_notes(quotes, errors, include_sources=show_source)
        heading = f"### 📈 {title.strip() or '实时行情'}"
        rows = [heading, ""]
        for q in quotes:
            label = format_label(q.display_name, q.market, q.contract_type)
            price_str = format_price(q.price, q.currency)
            change_str = format_change(
                q.change_pct,
                color_style=color_style,
                icon_style=icon_style,
            )
            rows.append(f"- **{label}** {price_str} ({change_str})")
        if notes:
            rows.append("")
            rows.extend(f"> {note}" for note in notes)
        return "\n".join(rows)

    notes = _collect_notes(quotes, errors, include_sources=False)
    lines = [
        format_quote_line(
            q,
            color_style=color_style,
            show_source=show_source,
            icon_style=icon_style,
        )
        for q in quotes
    ]
    lines.extend(notes)
    return "\n".join(lines)
