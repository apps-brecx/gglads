"""EmailTemplateRenderer — assembles a layout of brand-styled blocks into
production-grade, email-client-safe HTML.

Output is table-based, inline-CSS, mobile-responsive, max ~600px, with alt
text on images and a dark-mode-friendly palette. A plain-text fallback is
produced alongside. Blocks: hero, product_grid, single_product, text, button,
divider, footer.

The agent (or UI) passes a list of block dicts; render() returns
(html, plain_text). No external deps — pure string assembly keeps the output
deterministic and reviewable.
"""

from __future__ import annotations

import html
from typing import Any

WIDTH = 600


def _esc(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)


def _btn(label: str, url: str, color: str) -> str:
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:8px auto;"><tr><td align="center" bgcolor="{color}" '
        f'style="border-radius:8px;">'
        f'<a href="{_esc(url)}" target="_blank" '
        f'style="display:inline-block;padding:12px 28px;font-family:Arial,sans-serif;'
        f'font-size:15px;font-weight:bold;color:#ffffff;text-decoration:none;">'
        f'{_esc(label)}</a></td></tr></table>'
    )


class EmailTemplateRenderer:
    def __init__(self, brand_palette: list[str] | None = None) -> None:
        palette = brand_palette or []
        self.primary = palette[0] if palette else "#111111"
        self.accent = palette[1] if len(palette) > 1 else self.primary

    # ---- individual blocks --------------------------------------------
    def _hero(self, b: dict[str, Any]) -> str:
        img = b.get("image_url")
        head = _esc(b.get("headline", ""))
        sub = _esc(b.get("subhead", ""))
        img_html = (
            f'<img src="{_esc(img)}" width="{WIDTH}" alt="{_esc(b.get("alt", head))}" '
            f'style="display:block;width:100%;max-width:{WIDTH}px;height:auto;border:0;" />'
            if img else ""
        )
        return (
            f'<tr><td style="padding:0;">{img_html}</td></tr>'
            f'<tr><td style="padding:24px 32px 8px;font-family:Arial,sans-serif;">'
            f'<h1 style="margin:0;font-size:26px;line-height:1.2;color:{self.primary};">{head}</h1>'
            f'<p style="margin:8px 0 0;font-size:16px;color:#444444;">{sub}</p></td></tr>'
        )

    def _text(self, b: dict[str, Any]) -> str:
        body = _esc(b.get("text", "")).replace("\n", "<br/>")
        return (
            f'<tr><td style="padding:12px 32px;font-family:Arial,sans-serif;'
            f'font-size:16px;line-height:1.6;color:#333333;">{body}</td></tr>'
        )

    def _single_product(self, b: dict[str, Any]) -> str:
        p = b.get("product", {})
        img = p.get("image_url")
        img_html = (
            f'<img src="{_esc(img)}" width="280" alt="{_esc(p.get("title"))}" '
            f'style="display:block;width:100%;max-width:280px;height:auto;border-radius:8px;border:0;" />'
            if img else ""
        )
        price = f"${p['price']:.2f}" if p.get("price") is not None else ""
        cta = _btn(b.get("cta", "Shop now"), p.get("url", "#"), self.accent)
        return (
            f'<tr><td style="padding:16px 32px;font-family:Arial,sans-serif;" align="center">'
            f'{img_html}'
            f'<h2 style="margin:14px 0 4px;font-size:20px;color:{self.primary};">{_esc(p.get("title"))}</h2>'
            f'<p style="margin:0 0 10px;font-size:18px;color:#444;">{price}</p>'
            f'{cta}</td></tr>'
        )

    def _product_grid(self, b: dict[str, Any]) -> str:
        products = b.get("products", [])[:4]
        # Build one cell per product, then lay them out two-per-row.
        rows = ""
        flat = []
        for p in products:
            img = p.get("image_url")
            img_html = (
                f'<img src="{_esc(img)}" width="250" alt="{_esc(p.get("title"))}" '
                f'style="display:block;width:100%;height:auto;border-radius:8px;border:0;" />'
                if img else ""
            )
            price = f"${p['price']:.2f}" if p.get("price") is not None else ""
            flat.append(
                f'<td width="50%" valign="top" style="padding:10px;font-family:Arial,sans-serif;">'
                f'{img_html}'
                f'<p style="margin:8px 0 2px;font-size:15px;font-weight:bold;color:{self.primary};">{_esc(p.get("title"))}</p>'
                f'<p style="margin:0 0 6px;font-size:14px;color:#666;">{price}</p>'
                f'<a href="{_esc(p.get("url", "#"))}" style="font-size:13px;color:{self.accent};">Shop →</a></td>'
            )
        for i in range(0, len(flat), 2):
            pair = "".join(flat[i:i + 2])
            rows += f'<tr>{pair}</tr>'
        return (
            f'<tr><td style="padding:8px 22px;"><table role="presentation" width="100%" '
            f'cellpadding="0" cellspacing="0" border="0">{rows}</table></td></tr>'
        )

    def _button(self, b: dict[str, Any]) -> str:
        return f'<tr><td align="center" style="padding:12px 32px;">{_btn(b.get("label", "Shop now"), b.get("url", "#"), self.accent)}</td></tr>'

    def _divider(self, b: dict[str, Any]) -> str:
        return '<tr><td style="padding:8px 32px;"><hr style="border:0;border-top:1px solid #e5e5e5;margin:0;"/></td></tr>'

    def _footer(self, b: dict[str, Any]) -> str:
        name = _esc(b.get("brand_name", "Our brand"))
        return (
            f'<tr><td style="padding:24px 32px;font-family:Arial,sans-serif;'
            f'font-size:12px;line-height:1.5;color:#999999;" align="center">'
            f'{name}<br/>'
            f'You are receiving this because you subscribed.<br/>'
            f'<a href="{{{{ unsubscribe }}}}" style="color:#999999;">Unsubscribe</a>'
            f'</td></tr>'
        )

    _BLOCKS = {
        "hero": _hero,
        "text": _text,
        "single_product": _single_product,
        "product_grid": _product_grid,
        "button": _button,
        "divider": _divider,
        "footer": _footer,
    }

    # ---- public -------------------------------------------------------
    def render(self, layout: list[dict[str, Any]], *, preheader: str = "") -> tuple[str, str]:
        inner = ""
        for block in layout:
            kind = block.get("kind")
            fn = self._BLOCKS.get(kind)
            if fn:
                inner += fn(self, block)
        preheader_html = (
            f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{_esc(preheader)}</div>'
            if preheader else ""
        )
        html_doc = (
            '<!DOCTYPE html><html lang="en"><head>'
            '<meta charset="utf-8"/>'
            '<meta name="viewport" content="width=device-width, initial-scale=1.0"/>'
            '<meta name="color-scheme" content="light dark"/>'
            '<meta name="supported-color-schemes" content="light dark"/>'
            '<title></title></head>'
            '<body style="margin:0;padding:0;background:#f4f4f5;">'
            f'{preheader_html}'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="background:#f4f4f5;"><tr><td align="center" style="padding:24px 12px;">'
            f'<table role="presentation" width="{WIDTH}" cellpadding="0" cellspacing="0" border="0" '
            f'style="width:100%;max-width:{WIDTH}px;background:#ffffff;border-radius:12px;overflow:hidden;">'
            f'{inner}'
            '</table></td></tr></table></body></html>'
        )
        return html_doc, self.to_plain_text(layout)

    def to_plain_text(self, layout: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for b in layout:
            k = b.get("kind")
            if k == "hero":
                lines += [b.get("headline", ""), b.get("subhead", ""), ""]
            elif k == "text":
                lines += [b.get("text", ""), ""]
            elif k == "single_product":
                p = b.get("product", {})
                lines += [p.get("title", ""), p.get("url", ""), ""]
            elif k == "product_grid":
                for p in b.get("products", []):
                    lines.append(f"- {p.get('title','')}: {p.get('url','')}")
                lines.append("")
            elif k == "button":
                lines += [f"{b.get('label','')}: {b.get('url','')}", ""]
            elif k == "footer":
                lines += [b.get("brand_name", ""), "Unsubscribe: {{ unsubscribe }}"]
        return "\n".join(line for line in lines if line is not None).strip()
