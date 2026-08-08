"""Render the architecture diagram as standalone SVG, using official Azure icons.

    python scripts/render_architecture.py

Writes four files into docs/images: English and Chinese, each in a light and a
dark variant, selected in the READMEs by prefers-color-scheme.

Why a script instead of Mermaid: GitHub renders Mermaid inside a fixed-height
frame, and Mermaid cannot place product icons in a node without falling back to
foreignObject, which does not render when an SVG is loaded through an <img>.
Laying the diagram out here gives icon support, exact sizing, and output that is
plain SVG <text> and <image> — valid XML that any renderer can display.

The icon pack is downloaded on demand and never committed. Microsoft permits
these icons in architecture diagrams and documentation, and asks that they not
be distorted and that the product name appear near the icon; both hold here.
"""

from __future__ import annotations

import base64
import io
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "docs" / "images"
CACHE = Path.home() / ".cache" / "azure-architecture-icons"
ICON_ZIP_URL = "https://arch-center.azureedge.net/icons/Azure_Public_Service_Icons_V19.zip"

WIDTH, HEIGHT = 790, 1260
FONT_STACK = (
    "-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, sans-serif"
)
ICON = 46

# Only the leaf filename is matched, so a pack reshuffle does not break this.
ICON_FILES = {
    "function": "10029-icon-service-Function-Apps.svg",
    "eventhub": "00039-icon-service-Event-Hubs.svg",
    "storage": "10086-icon-service-Storage-Accounts.svg",
    "appinsights": "00012-icon-service-Application-Insights.svg",
    "alerts": "00002-icon-service-Alerts.svg",
    "staticapp": "01007-icon-service-Static-Apps.svg",
    "keyvault": "10245-icon-service-Key-Vaults.svg",
    "powerbi": "03332-icon-service-Power-BI-Embedded.svg",
    "openai": "03438-icon-service-Azure-OpenAI.svg",
    "search": "10044-icon-service-Cognitive-Search.svg",
}

# weatherapi.com is a third-party service and the HTTP endpoints are ordinary
# Function App routes, not API Management. Microsoft asks that its product icons
# not stand in for someone else's product, and borrowing an APIM icon would also
# put a service in the diagram that this architecture does not use — so the
# external source gets a drawn shape and the API keeps the Functions icon.
DOCUMENT_ICON = (
    '<path d="M {x0} {y0} h 22 l 10 10 v 30 a 3 3 0 0 1 -3 3 h -26'
    ' a 3 3 0 0 1 -3 -3 v -37 a 3 3 0 0 1 3 -3 z"'
    ' fill="none" stroke="{c}" stroke-width="1.6"/>'
    '<path d="M {x1} {y0} v 10 h 10" fill="none" stroke="{c}" stroke-width="1.6"/>'
    '<path d="M {x2} {y1} h 16 M {x2} {y2} h 16 M {x2} {y3} h 10" stroke="{c}" stroke-width="1.4"/>'
)

EXTERNAL_ICON = (
    '<circle cx="{cx}" cy="{cy}" r="20" fill="none" stroke="{c}" stroke-width="1.6"/>'
    '<ellipse cx="{cx}" cy="{cy}" rx="8.5" ry="20" fill="none" stroke="{c}" stroke-width="1.6"/>'
    '<path d="M {x0} {cy} H {x1}" stroke="{c}" stroke-width="1.6"/>'
    '<path d="M {x2} {y2} Q {cx} {y3} {x3} {y2}" fill="none" stroke="{c}" stroke-width="1.6"/>'
    '<path d="M {x2} {y4} Q {cx} {y5} {x3} {y4}" fill="none" stroke="{c}" stroke-width="1.6"/>'
)

THEMES = {
    "light": {"text": "#1f2328", "muted": "#59636e", "line": "#59636e",
              "group": "#f6f8fa", "groupLine": "#d1d9e0", "card": "#ffffff",
              "cardLine": "#d1d9e0"},
    "dark": {"text": "#e6edf3", "muted": "#9198a1", "line": "#9198a1",
             "group": "#161b22", "groupLine": "#3d444d", "card": "#0d1117",
             "cardLine": "#3d444d"},
}


@dataclass
class Node:
    id: str
    x: int
    y: int  # icon centre
    icon: str
    en: tuple[str, ...]
    zh: tuple[str, ...]


@dataclass
class Group:
    x: int
    y: int
    w: int
    h: int
    en: str
    zh: str


@dataclass
class Edge:
    a: str
    b: str
    dashed: bool = False
    en: str = ""
    zh: str = ""
    via: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    label_at: tuple[int, int] | None = None
    # Force which side an edge leaves or enters by, when the geometry alone
    # would pick a side that produces an awkward diagonal.
    a_side: str = ""
    b_side: str = ""


NODES = [
    Node("api", 140, 60, "external", ("weatherapi.com",), ("weatherapi.com",)),
    Node("kv", 325, 60, "keyvault", ("Key Vault", "API key"), ("Key Vault", "API 密钥")),
    Node("cur", 140, 196, "function", ("ingest_current", "timer · 30s"),
         ("ingest_current", "定时 · 30 秒")),
    Node("fc", 325, 196, "function", ("ingest_forecast", "timer · 30min"),
         ("ingest_forecast", "定时 · 30 分钟")),
    Node("ai", 590, 215, "appinsights", ("Application Insights",), ("Application Insights",)),
    Node("eh", 140, 355, "eventhub", ("Event Hubs", "buffer · 1 day"),
         ("Event Hubs", "缓冲区 · 保留 1 天")),
    Node("alert", 590, 360, "alerts", ("Azure Monitor", "3 alert rules"),
         ("Azure Monitor", "3 条告警规则")),
    Node("arc", 140, 490, "function", ("archive_to_bronze", "Event Hub trigger"),
         ("archive_to_bronze", "Event Hub 触发")),
    Node("bronze", 140, 640, "storage", ("bronze", "raw JSONL"), ("bronze", "原始 JSONL")),
    Node("curate", 315, 640, "function", ("curate", "timer · hourly"),
         ("curate", "定时 · 每小时")),
    Node("silver", 490, 640, "storage", ("silver", "Parquet"), ("silver", "Parquet")),
    Node("serving", 665, 640, "storage", ("serving", "aggregated JSON"),
         ("serving", "聚合 JSON")),
    Node("pbi", 490, 810, "powerbi", ("Power BI",), ("Power BI",)),
    Node("http", 665, 810, "function", ("HTTP API", "/api/latest …"),
         ("HTTP API", "/api/latest …")),
    Node("swa", 665, 920, "staticapp", ("Static Web App", "public dashboard"),
         ("Static Web App", "公开看板")),
    Node("knowledge", 140, 1080, "document", ("knowledge index", "23 chunks · versioned"),
         ("知识索引", "23 个 chunk · 带版本")),
    Node("advice", 325, 1080, "function", ("api_advice", "rules → retrieve → validate"),
         ("api_advice", "规则 → 检索 → 校验")),
    Node("openai", 530, 1080, "openai", ("Azure OpenAI", "optional"),
         ("Azure OpenAI", "可选")),
    Node("search", 690, 1080, "search", ("AI Search", "optional"), ("AI Search", "可选")),
]

GROUPS = [
    Group(55, 140, 355, 118, "Ingestion", "采集"),
    Group(470, 155, 240, 285, "Monitoring", "监控"),
    Group(55, 585, 675, 118, "ADLS Gen2 — data lake", "ADLS Gen2 数据湖"),
    Group(55, 1010, 675, 200, "Advice cards — on the v1.1 and v1.2 tags",
          "建议卡片 —— 在 v1.1 与 v1.2 标签上"),
]

EDGES = [
    Edge("api", "cur"),
    Edge("kv", "fc", dashed=True),
    Edge("cur", "eh"),
    Edge("fc", "eh", via=((325, 320), (140, 320))),
    Edge("cur", "ai", dashed=True, en="threshold breaches", zh="阈值突破",
         a_side="bottom", b_side="left",
         via=((140, 285), (440, 285), (440, 215)), label_at=(300, 285)),
    Edge("ai", "alert"),
    Edge("eh", "arc"),
    Edge("arc", "bronze", a_side="bottom", b_side="left",
         via=((140, 545), (32, 545), (32, 640))),
    Edge("bronze", "curate"),
    Edge("curate", "silver"),
    Edge("curate", "serving", a_side="bottom", b_side="right",
         via=((315, 735), (718, 735), (718, 640))),
    Edge("silver", "pbi"),
    Edge("serving", "http"),
    Edge("http", "swa"),
    # The advice endpoint reads the same serving document the dashboard does;
    # it adds no storage of its own.
    Edge("serving", "advice", a_side="right", b_side="top",
         en="weather snapshot", zh="天气快照",
         via=((757, 640), (757, 985), (325, 985)), label_at=(560, 985)),
    Edge("knowledge", "advice"),
    # Dashed because both are optional. With neither configured the card still
    # appears, written by the v1.1 template.
    Edge("advice", "openai", dashed=True),
    Edge("advice", "search", dashed=True, a_side="bottom", b_side="bottom",
         via=((325, 1150), (690, 1150))),
    # Leaves downward, not left: a left exit would run straight through the
    # knowledge node and land on top of the knowledge -> advice edge.
    Edge("advice", "swa", a_side="bottom", b_side="left",
         via=((325, 1180), (30, 1180), (30, 900), (600, 900))),
]


def fetch_icons() -> dict[str, str]:
    """Return {key: base64 data URI}. Downloads the pack once, then caches it."""
    CACHE.mkdir(parents=True, exist_ok=True)
    archive = CACHE / "icons.zip"
    if not archive.exists():
        print(f"downloading {ICON_ZIP_URL} …")
        with urllib.request.urlopen(ICON_ZIP_URL, timeout=120) as response:
            archive.write_bytes(response.read())

    wanted = {v: k for k, v in ICON_FILES.items()}
    found: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(archive.read_bytes())) as zf:
        for info in zf.namelist():
            leaf = info.rsplit("/", 1)[-1]
            key = wanted.get(leaf)
            if key and key not in found:
                data = zf.read(info)
                found[key] = "data:image/svg+xml;base64," + base64.b64encode(data).decode()

    missing = set(ICON_FILES) - set(found)
    if missing:
        raise SystemExit(f"icons not found in the pack: {sorted(missing)}")
    return found


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def anchor(node: Node, towards: Node, side: str = "") -> tuple[int, int]:
    """Where an edge leaves a node: its icon edge, or below its label."""
    half = ICON // 2
    below, above = node.y + half + 30, node.y - half - 4  # below clears both label lines
    if side == "bottom":
        return node.x, below
    if side == "top":
        return node.x, above
    if side == "left":
        return node.x - half - 4, node.y
    if side == "right":
        return node.x + half + 4, node.y
    if towards.y > node.y + half:
        return node.x, below
    if towards.y < node.y - half:
        return node.x, above
    return (node.x + half + 4 if towards.x > node.x else node.x - half - 4), node.y


def render(lang: str, theme_name: str, icons: dict[str, str]) -> str:
    t = THEMES[theme_name]
    by_id = {n.id: n for n in NODES}
    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" width="{WIDTH}" height="{HEIGHT}" '
        f'font-family="{FONT_STACK}">',
        f'<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
        f'markerHeight="6" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{t["line"]}"/></marker></defs>',
    ]

    for g in GROUPS:
        title = g.en if lang == "en" else g.zh
        out.append(
            f'<rect x="{g.x}" y="{g.y}" width="{g.w}" height="{g.h}" rx="10" '
            f'fill="{t["group"]}" stroke="{t["groupLine"]}" stroke-dasharray="5 4"/>'
        )
        out.append(
            f'<text x="{g.x + 12}" y="{g.y + 20}" font-size="12" font-weight="600" '
            f'fill="{t["muted"]}">{esc(title)}</text>'
        )

    for e in EDGES:
        a, b = by_id[e.a], by_id[e.b]
        start = anchor(a, b, e.a_side)
        end = anchor(b, a, e.b_side)
        points = [start, *e.via, end]
        d = f'M {points[0][0]} {points[0][1]} ' + " ".join(
            f"L {x} {y}" for x, y in points[1:]
        )
        dash = ' stroke-dasharray="4 4"' if e.dashed else ""
        out.append(
            f'<path d="{d}" fill="none" stroke="{t["line"]}" stroke-width="1.5"'
            f'{dash} marker-end="url(#arrow)"/>'
        )
        label = e.en if lang == "en" else e.zh
        if label:
            mx, my = e.label_at or ((start[0] + end[0]) // 2, (start[1] + end[1]) // 2)
            width = len(label) * (6.6 if lang == "en" else 12) + 10
            out.append(
                f'<rect x="{mx - width / 2:.0f}" y="{my - 9}" width="{width:.0f}" height="16" '
                f'rx="3" fill="{t["group"]}"/>'
                f'<text x="{mx}" y="{my + 3}" font-size="10.5" text-anchor="middle" '
                f'fill="{t["muted"]}">{esc(label)}</text>'
            )

    for n in NODES:
        lines = n.en if lang == "en" else n.zh
        if n.icon == "document":
            out.append(DOCUMENT_ICON.format(
                c=t["muted"],
                x0=n.x - 16, y0=n.y - 21, x1=n.x + 6,
                x2=n.x - 8, y1=n.y - 2, y2=n.y + 6, y3=n.y + 14,
            ))
        elif n.icon == "external":
            out.append(EXTERNAL_ICON.format(
                cx=n.x, cy=n.y, c=t["muted"],
                x0=n.x - 20, x1=n.x + 20,
                x2=n.x - 17, x3=n.x + 17,
                y2=n.y - 7, y3=n.y - 14, y4=n.y + 7, y5=n.y + 14,
            ))
        else:
            out.append(
                f'<image xlink:href="{icons[n.icon]}" x="{n.x - ICON // 2}" '
                f'y="{n.y - ICON // 2}" width="{ICON}" height="{ICON}"/>'
            )
        out.append(
            f'<text x="{n.x}" y="{n.y + ICON // 2 + 15}" font-size="12" font-weight="600" '
            f'text-anchor="middle" fill="{t["text"]}">{esc(lines[0])}</text>'
        )
        if len(lines) > 1:
            out.append(
                f'<text x="{n.x}" y="{n.y + ICON // 2 + 28}" font-size="10.5" '
                f'text-anchor="middle" fill="{t["muted"]}">{esc(lines[1])}</text>'
            )

    out.append("</svg>")
    return "".join(out)


def main() -> int:
    icons = fetch_icons()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for lang, suffix in (("en", ""), ("zh", "-zh")):
        for theme in ("light", "dark"):
            svg = render(lang, theme, icons)
            ET.fromstring(svg)  # never ship an SVG that will not parse
            path = OUT_DIR / f"architecture{suffix}-{theme}.svg"
            path.write_text(svg, encoding="utf-8")
            print(f"{path.relative_to(REPO)}  {len(svg) // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
