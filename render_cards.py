#!/usr/bin/env python3
"""
render_cards.py - renders the profile README as a stack of animated SVG cards.

    python render_cards.py              # animated cards  -> cards/
    STATIC=1 python render_cards.py     # final frame     -> cards-static/ (+ preview.html)

Everything is built for <img> embedding, where SVG runs in "secure static mode":
no scripts, no external requests, no links. So each card uses only
  * one inline <style> block (CSS keyframes, @font-face with base64 WOFF data URIs)
  * SMIL <animate>/<animateTransform> for the ambient bits
  * data: URIs for the optional portrait
Every animation is switched off under `prefers-reduced-motion: reduce`.
(CSS animations by `animation: none`; SMIL by hiding an animated group and showing
a static twin, because CSS cannot stop SMIL.)

Inputs
  data/contributions.json   refreshed daily by .github/workflows/cards.yml
  data/repos.json           featured repos (curated text + metadata the Action refreshes)
  fonts/Inter-*.otf         subset per card with pyftsubset
  assets/portrait.*         optional
Only dependency: fonttools (for pyftsubset and glyph metrics). Pillow is used if present.
"""
from __future__ import annotations

import argparse
import base64
import calendar
import datetime as dt
import html
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.dom.minidom
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = os.environ.get("STATIC", "").strip().lower() not in ("", "0", "false", "no")

# --------------------------------------------------------------------------- #
# Content (edit freely)
# --------------------------------------------------------------------------- #
PROFILE = {
    "name": "Debesh Panda",
    "tagline": "Backend & full-stack engineer building reliable distributed systems.",
    "status": "Building PayFlow",
}

TILES = [
    {"slug": "portfolio", "icon": "globe", "label": "Portfolio", "value": "dev-codes-chi.vercel.app"},
    {"slug": "linkedin", "icon": "in", "label": "LinkedIn", "value": "Connect on LinkedIn"},
    {"slug": "email", "icon": "mail", "label": "Email", "value": "itsdkp555@gmail.com"},
]

# --------------------------------------------------------------------------- #
# Theme. NOTE: seeded from the existing README (Inter, #3B82F6 brand blue, the
# #111827 Portfolio badge), NOT read from the website. Swap these values and
# every card follows; everything else is derived from them.
# --------------------------------------------------------------------------- #
THEME = {
    "brand": "#3B82F6",
    "card": "#111827",     # card background
    "card_hi": "#18223A",  # top-left of the card gradient
    "raised": "#1C2740",   # chips, icon wells
    "border": "#2A3652",
    "text": "#F3F4F6",
    "text2": "#CBD5E1",
    "muted": "#9CA3AF",
}

LANG_COLORS = {
    "TypeScript": "#3178C6", "JavaScript": "#F1E05A", "Go": "#00ADD8",
    "Python": "#3572A5", "C++": "#F34B7D", "Rust": "#DEA584", "Java": "#B07219",
}

FONT_FILES = {400: "Inter-Regular.otf", 600: "Inter-SemiBold.otf", 800: "Inter-ExtraBold.otf"}
FONT_DIRS = [ROOT / "fonts", Path("/usr/share/fonts/opentype/inter"),
             Path("/usr/share/fonts/truetype/inter"), Path("/usr/share/fonts/inter")]
FAMILY = "InterCard"
W = 900  # every wide card is 900 units wide; tiles are 300


# --------------------------------------------------------------------------- #
# Colour helpers
# --------------------------------------------------------------------------- #
def _rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def mix(a: str, b: str, t: float) -> str:
    """Linear mix a->b by t (0..1)."""
    ra, rb = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ra, rb))


T = dict(THEME)
T["brand_soft"] = mix(T["brand"], "#ffffff", 0.28)   # brand as *text* / strokes on dark
T["grid"] = mix(T["card"], T["text"], 0.09)
T["axis"] = mix(T["card"], T["text"], 0.22)
# Sequential ramp for the heatmap: one hue, lightness rises with value (dark-surface anchor).
# Level 0 is "no contributions" and deliberately sits near the surface. Levels 1-4 were checked
# with the dataviz validator (--ordinal --mode dark): monotone, step gaps >= 0.06 L, and the
# lightest step clears the surface by >= 2:1. Re-run it if you change the brand colour.
RAMP = [
    mix(T["card"], T["text"], 0.07),
    mix(T["card"], T["brand"], 0.50),
    mix(T["card"], T["brand"], 0.72),
    T["brand"],
    mix(T["brand"], "#ffffff", 0.42),
]


def n(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def attr(s: str) -> str:
    return html.escape(s, quote=True)


# --------------------------------------------------------------------------- #
# Fonts: metrics for layout + pyftsubset for embedding
# --------------------------------------------------------------------------- #
class Fonts:
    def __init__(self) -> None:
        from fontTools.ttLib import TTFont  # fonttools is already required by pyftsubset

        self.path: dict[int, Path] = {}
        self._m: dict[int, tuple] = {}
        for weight, fname in FONT_FILES.items():
            p = next((d / fname for d in FONT_DIRS if (d / fname).exists()), None)
            if p is None:
                sys.exit(f"Missing font {fname}. Put it in {ROOT / 'fonts'} (Inter, SIL OFL).")
            f = TTFont(str(p), lazy=True)
            self.path[weight] = p
            self._m[weight] = (f.getBestCmap(), f["hmtx"].metrics, f["head"].unitsPerEm)

    def clean(self, text: str) -> str:
        """Drop characters Inter has no glyph for (e.g. emoji) so nothing falls back."""
        cmap = self._m[400][0]
        return "".join(ch for ch in text if ord(ch) in cmap)

    def width(self, text: str, weight: int, size: float, ls: float = 0.0) -> float:
        cmap, hmtx, upm = self._m[weight]
        total = 0
        for ch in text:
            g = cmap.get(ord(ch))
            total += hmtx[g][0] if g in hmtx else upm * 0.5
        return total * size / upm + ls * len(text)

    def fit(self, text: str, weight: int, max_w: float, size: float, min_size: float) -> float:
        while size > min_size and self.width(text, weight, size) > max_w:
            size -= 1
        return size

    def wrap(self, text: str, weight: int, size: float, max_w: float, max_lines: int) -> list[str]:
        """Greedy word wrap; if the text needs more than max_lines the last line ends in an ellipsis."""
        lines: list[str] = []
        cur = ""
        for word in text.split():
            trial = (cur + " " + word).strip()
            if cur and self.width(trial, weight, size) > max_w:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
        if len(lines) <= max_lines:
            return lines
        last = " ".join(lines[max_lines - 1:])
        while last and self.width(last + "…", weight, size) > max_w:
            last = last[:-1].rstrip()
        return lines[: max_lines - 1] + [last + "…"]


F = Fonts()
PYFTSUBSET = [shutil.which("pyftsubset")] if shutil.which("pyftsubset") else [sys.executable, "-m", "fontTools.subset"]


def subset_woff_b64(weight: int, chars: set[str]) -> str:
    """pyftsubset the weight down to `chars` and return it as base64 WOFF."""
    chars = chars | {" ", "?", "…"}
    with tempfile.TemporaryDirectory() as td:
        txt, out = Path(td, "chars.txt"), Path(td, "f.woff")
        txt.write_text("".join(sorted(chars)), encoding="utf-8")
        cmd = PYFTSUBSET + [
            str(F.path[weight]), f"--text-file={txt}", f"--output-file={out}",
            "--flavor=woff", "--layout-features+=tnum", "--no-hinting", "--desubroutinize",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 or not out.exists():
            sys.exit(f"pyftsubset failed for weight {weight}:\n{res.stderr}")
        return base64.b64encode(out.read_bytes()).decode("ascii")


# --------------------------------------------------------------------------- #
# CSS
# --------------------------------------------------------------------------- #
BASE_CSS = (
    "text{font-family:'%s',system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;"
    "text-rendering:geometricPrecision}" % FAMILY
)

ANIM_CSS = """
.fade{animation:fade .6s ease-out both}
.rise{animation:rise .7s cubic-bezier(.2,.7,.2,1) both}
.rise6{animation:rise6 .5s cubic-bezier(.2,.7,.2,1) both}
.pop{animation:pop .5s cubic-bezier(.2,.8,.2,1) both;transform-box:fill-box;transform-origin:center}
.grow{animation:grow .8s cubic-bezier(.2,.7,.2,1) both;transform-box:fill-box;transform-origin:50% 100%}
.sweep{animation:sweep .7s cubic-bezier(.2,.7,.2,1) both;transform-box:fill-box;transform-origin:0 50%}
.slide{animation:slide .6s cubic-bezier(.2,.7,.2,1) both}
.r{display:none}
@keyframes fade{from{opacity:0}}
@keyframes rise{from{opacity:0;transform:translateY(14px)}}
@keyframes rise6{from{opacity:0;transform:translateY(6px)}}
@keyframes pop{from{opacity:0;transform:scale(.6)}}
@keyframes grow{from{transform:scaleY(0)}}
@keyframes sweep{from{transform:scaleX(0)}}
@keyframes slide{from{opacity:0;transform:translate(-6px,6px)}}
@media (prefers-reduced-motion:reduce){
*,*::before,*::after{animation:none!important;transition:none!important}
.m{display:none}
.r{display:inline}
}
"""


# --------------------------------------------------------------------------- #
# Card: a tiny SVG builder that tracks which glyphs each weight needs
# --------------------------------------------------------------------------- #
class Card:
    def __init__(self, name: str, w: int, h: int, base: float, title: str, desc: str) -> None:
        self.name, self.w, self.h, self.base = name, w, h, base
        self.title, self.desc = title, desc
        self.defs: list[str] = []
        self.body: list[str] = []
        self.used: dict[int, set[str]] = defaultdict(set)

    # -- animation helpers -------------------------------------------------- #
    def a(self, kind: str | None, offset: float = 0.0) -> str:
        """class + staggered delay attributes (empty in STATIC mode)."""
        if STATIC or not kind:
            return ""
        return f' class="{kind}" style="animation-delay:{n(self.base + offset)}s"'

    def twin(self, static: str, animated: str) -> str:
        """SMIL cannot be disabled by CSS, so ship an animated and a static copy."""
        if STATIC:
            return static
        return f'<g class="m">{animated}</g><g class="r">{static}</g>'

    # -- primitives ---------------------------------------------------------- #
    def add(self, markup: str) -> None:
        self.body.append(markup)

    def text(self, x: float, y: float, s: str, size: float, weight: int = 400, fill: str | None = None,
             anchor: str = "start", ls: float = 0.0, anim: tuple[str, float] | None = None,
             style: str = "") -> str:
        s = F.clean(s)
        self.used[weight].update(s)
        extra = f' letter-spacing="{n(ls)}"' if ls else ""
        extra += f' text-anchor="{anchor}"' if anchor != "start" else ""
        extra += f' style="{style}"' if style and (STATIC or not anim) else ""
        a = self.a(*anim) if anim else ""
        if anim and style and not STATIC:
            a = a.replace('style="', f'style="{style};')
        return (f'<text x="{n(x)}" y="{n(y)}" font-size="{n(size)}" font-weight="{weight}" '
                f'fill="{fill or T["text"]}"{extra}{a}>{esc(s)}</text>')

    def bg(self, rx: int = 22, extra_defs: str = "") -> None:
        self.defs.append(
            f'<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
            f'<stop offset="0" stop-color="{T["card_hi"]}"/><stop offset="1" stop-color="{T["card"]}"/></linearGradient>'
            f'<clipPath id="clip"><rect width="{self.w}" height="{self.h}" rx="{rx}"/></clipPath>' + extra_defs)
        self.add(f'<rect x=".5" y=".5" width="{self.w - 1}" height="{self.h - 1}" rx="{rx}" fill="url(#bg)" '
                 f'stroke="{T["border"]}"{self.a("fade", 0)}/>')

    # -- output -------------------------------------------------------------- #
    def svg(self) -> str:
        faces = "".join(
            f"@font-face{{font-family:'{FAMILY}';font-style:normal;font-weight:{wt};"
            f"src:url(data:font/woff;base64,{subset_woff_b64(wt, chars)}) format('woff')}}\n"
            for wt, chars in sorted(self.used.items()) if chars)
        css = faces + BASE_CSS + ("" if STATIC else ANIM_CSS)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
            f'viewBox="0 0 {self.w} {self.h}" role="img" aria-labelledby="t d">\n'
            f'<title id="t">{esc(self.title)}</title><desc id="d">{esc(self.desc)}</desc>\n'
            f"<style><![CDATA[\n{css}]]></style>\n"
            f"<defs>{''.join(self.defs)}</defs>\n" + "\n".join(self.body) + "\n</svg>\n")


# --------------------------------------------------------------------------- #
# Small icon drawing (all stroke-based, no glyph dependency)
# --------------------------------------------------------------------------- #
def icon(kind: str, x: float, y: float, color: str, sw: float = 2.0) -> str:
    """40x40 icon box at (x, y)."""
    g = f'<g transform="translate({n(x)} {n(y)})" fill="none" stroke="{color}" stroke-width="{n(sw)}" stroke-linecap="round" stroke-linejoin="round">'
    if kind == "globe":
        g += '<circle cx="20" cy="20" r="9.5"/><ellipse cx="20" cy="20" rx="4.2" ry="9.5"/><path d="M10.5 20h19"/>'
    elif kind == "mail":
        g += '<rect x="10.5" y="13.5" width="19" height="13" rx="2.8"/><path d="M11.5 15.5l8.5 6.3 8.5-6.3"/>'
    elif kind == "in":
        g += ('<rect x="10.5" y="10.5" width="19" height="19" rx="4.5"/>'
              f'<circle cx="15.6" cy="16" r="1.1" fill="{color}" stroke="none"/>'
              '<path d="M15.6 19.4V25M19.8 25v-5.6M19.8 21.6c0-1.7 1.3-2.4 2.6-2.4 1.7 0 2.6 1.1 2.6 2.9V25"/>')
    return g + "</g>"


def arrow_up_right(x: float, y: float, color: str, s: float = 12) -> str:
    return (f'<path d="M{n(x)} {n(y + s)}L{n(x + s)} {n(y)}M{n(x + 3)} {n(y)}H{n(x + s)}V{n(y + s - 3)}" fill="none" '
            f'stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>')


def star_icon(cx: float, cy: float, r: float, color: str) -> str:
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.45
        pts.append(f"{n(cx + rad * math.cos(ang))},{n(cy + rad * math.sin(ang))}")
    return f'<polygon points="{" ".join(pts)}" fill="{color}" stroke="{color}" stroke-width="1" stroke-linejoin="round"/>'


def fork_icon(x: float, y: float, color: str) -> str:
    return (f'<g transform="translate({n(x)} {n(y)})" fill="none" stroke="{color}" stroke-width="1.6" stroke-linecap="round">'
            '<circle cx="2.5" cy="2.5" r="1.9"/><circle cx="11.5" cy="2.5" r="1.9"/><circle cx="7" cy="12.5" r="1.9"/>'
            '<path d="M2.5 4.4V6c0 1.4 1.2 2.4 2.6 2.4h3.8c1.4 0 2.6-1 2.6-2.4V4.4M7 8.4v2.2"/></g>')


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def fmt_date(d: dt.date, year: bool = True) -> str:
    return f"{d.day} {d.strftime('%b')}" + (f" {d.year}" if year else "")


def load_contrib(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    days = sorted((dt.date.fromisoformat(d["date"]), int(d["count"])) for d in raw["days"])
    if not days:
        sys.exit(f"{path} has no days")
    last = days[-1][0]
    total = sum(c for _, c in days)
    active = sum(1 for _, c in days if c > 0)
    busiest = max(days, key=lambda x: (x[1], -x[0].toordinal()))

    # current streak: GitHub-style, today may still be empty without breaking it
    i = len(days) - 1
    if days[i][1] == 0:
        i -= 1
    cur, streak_start = 0, None
    while i >= 0 and days[i][1] > 0 and (cur == 0 or (days[i + 1][0] - days[i][0]).days == 1):
        cur += 1
        streak_start = days[i][0]
        i -= 1
    longest = run = 0
    for k, (d, c) in enumerate(days):
        contiguous = k > 0 and (d - days[k - 1][0]).days == 1
        run = (run + 1 if contiguous else 1) if c > 0 else 0
        longest = max(longest, run)

    # contributions per calendar month: the 12 most recent. A month we only hold the tail of
    # (data starts mid-month) is dropped, because its total would not be comparable.
    first = days[0][0]
    months: list[tuple[int, int]] = []
    y, m = last.year, last.month
    for _ in range(12):
        months.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()
    months = [(y, m) for (y, m) in months if dt.date(y, m, 1) >= first]
    per_month = defaultdict(int)
    for d, c in days:
        per_month[(d.year, d.month)] += c
    month_rows = [{"y": y, "m": m, "label": dt.date(y, m, 1).strftime("%b"), "count": per_month[(y, m)],
                   "partial": (y, m) == (last.year, last.month) and last.day < calendar.monthrange(y, m)[1]}
                  for (y, m) in months]
    return {"raw": raw, "days": days, "last": last, "total": total, "active": active, "busiest": busiest,
            "streak": cur, "streak_start": streak_start, "longest": longest, "months": month_rows,
            "sample": bool(raw.get("sample")), "generated": raw.get("generated") or last.isoformat()}


def load_portrait() -> tuple[str, str] | None:
    for ext in ("png", "jpg", "jpeg", "webp"):
        p = ROOT / "assets" / f"portrait.{ext}"
        if not p.exists():
            continue
        try:
            from PIL import Image  # optional: crop to a square and shrink for a light card

            im = Image.open(p).convert("RGB")
            s = min(im.size)
            l, t = (im.width - s) // 2, (im.height - s) // 2
            im = im.crop((l, t, l + s, t + s)).resize((360, 360))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=86, optimize=True)
            return "image/jpeg", base64.b64encode(buf.getvalue()).decode()
        except ImportError:
            data = p.read_bytes()
            if len(data) > 250_000:
                print(f"warning: {p.name} is {len(data) // 1024} KB and Pillow is missing; "
                      "the hero card will be heavy. Resize it to ~360px.", file=sys.stderr)
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}[ext]
            return mime, base64.b64encode(data).decode()
    return None


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #
def sample_tag(c: Card, x_right: float, y: float) -> None:
    label = "Sample data until the Action runs"
    w = F.width(F.clean(label), 600, 11, 0.4) + 20
    c.add(f'<rect x="{n(x_right - w)}" y="{n(y - 14)}" width="{n(w)}" height="22" rx="11" fill="none" stroke="{T["border"]}"/>')
    c.add(c.text(x_right - w / 2, y + 1, label, 11, 600, T["muted"], "middle", 0.4))


def card_hero(base: float) -> Card:
    portrait = load_portrait()
    H = 276
    c = Card("hero", W, H, base,
             f"{PROFILE['name']} - {PROFILE['tagline']}", f"{PROFILE['name']}. {PROFILE['tagline']} {PROFILE['status']}.")
    pcx, pr = 752, 84
    cy = H // 2 + 6
    c.bg(22, (
        f'<radialGradient id="orb"><stop offset="0" stop-color="{T["brand"]}" stop-opacity=".40"/>'
        f'<stop offset="1" stop-color="{T["brand"]}" stop-opacity="0"/></radialGradient>'
        f'<pattern id="dots" width="22" height="22" patternUnits="userSpaceOnUse">'
        f'<circle cx="1.5" cy="1.5" r="1.1" fill="{T["text"]}" fill-opacity=".07"/></pattern>'
        f'<clipPath id="pclip"><circle r="{pr}"/></clipPath>'))
    # ambient layer: dot grid + two drifting glows
    orb_a = f'<circle cx="760" cy="40" r="230" fill="url(#orb)"/>'
    orb_b = f'<circle cx="90" cy="330" r="190" fill="url(#orb)" opacity=".7"/>'
    drift = lambda dx, dy, dur: (f'<animateTransform attributeName="transform" type="translate" values="0 0;{dx} {dy};0 0" '
                                 f'dur="{dur}s" repeatCount="indefinite" calcMode="spline" keySplines=".4 0 .6 1;.4 0 .6 1"/>')
    ambient = c.twin(f'<g>{orb_a}</g><g>{orb_b}</g>',
                     f'<g>{orb_a}{drift(-22, 14, 16)}</g><g>{orb_b}{drift(26, -12, 19)}</g>')
    c.add(f'<g clip-path="url(#clip)"><rect width="{W}" height="{H}" fill="url(#dots)"/>{ambient}</g>')

    left = 56
    right_edge = (pcx - pr - 40) if portrait else (W - 56)
    max_w = right_edge - left

    # status pill with a pulsing dot
    status = F.clean(PROFILE["status"])
    pw = 16 + 18 + F.width(status, 600, 13) + 8
    c.add(f'<rect x="{left}" y="40" width="{n(pw)}" height="30" rx="15" fill="{T["raised"]}" stroke="{T["border"]}"{c.a("fade", 0.1)}/>')
    ring = (f'<circle cx="{left + 17}" cy="55" r="4" fill="none" stroke="{T["brand_soft"]}" stroke-width="1.5">'
            '<animate attributeName="r" values="4;11" dur="2.4s" repeatCount="indefinite"/>'
            '<animate attributeName="opacity" values=".7;0" dur="2.4s" repeatCount="indefinite"/></circle>')
    c.add(c.twin("", ring))
    c.add(f'<circle cx="{left + 17}" cy="55" r="4" fill="{T["brand_soft"]}"{c.a("pop", 0.15)}/>')
    c.add(c.text(left + 32, 60, status, 13, 600, T["text2"], anim=("fade", 0.2)))

    # name
    name = F.clean(PROFILE["name"])
    size = F.fit(name, 800, max_w, 66, 40)
    c.add(c.text(left - 2, 150, name, size, 800, T["text"], ls=-1.2, anim=("rise", 0.25)))
    c.add(f'<rect x="{left}" y="170" width="64" height="4" rx="2" fill="{T["brand"]}"{c.a("sweep", 0.7)}/>')

    # one line about me (wraps to two lines only when a portrait narrows the column)
    lines = F.wrap(F.clean(PROFILE["tagline"]), 400, 20, max_w, 2)
    for i, line in enumerate(lines):
        c.add(c.text(left, 212 + i * 30, line, 20, 400, T["text2"], anim=("rise", 0.5 + i * 0.1)))

    if portrait:
        mime, b64 = portrait
        c.add(f'<g{c.a("pop", 0.4)}><g transform="translate({pcx} {cy})">'
              f'<circle r="{pr + 12}" fill="none" stroke="{T["brand"]}" stroke-opacity=".28" stroke-width="2"/>'
              f'<image href="data:{mime};base64,{b64}" x="{-pr}" y="{-pr}" width="{2 * pr}" height="{2 * pr}" '
              f'preserveAspectRatio="xMidYMid slice" clip-path="url(#pclip)"/>'
              f'<circle r="{pr}" fill="none" stroke="{T["brand"]}" stroke-width="3"/></g></g>')
    return c


def card_tile(base: float, tile: dict) -> Card:
    Wt, Ht = 300, 128
    c = Card(f"tile-{tile['slug']}", Wt, Ht, base, f"{tile['label']}: {tile['value']}", f"Link to {tile['label']}: {tile['value']}")
    c.bg(18, f'<radialGradient id="orb" cx="1" cy="0" r="1"><stop offset="0" stop-color="{T["brand"]}" stop-opacity=".30"/>'
             f'<stop offset="1" stop-color="{T["brand"]}" stop-opacity="0"/></radialGradient>')
    c.add(f'<g clip-path="url(#clip)"><rect width="{Wt}" height="{Ht}" fill="url(#orb)"{c.a("fade", 0.1)}/></g>')
    c.add(f'<rect x="24" y="20" width="44" height="44" rx="12" fill="{T["raised"]}" stroke="{T["border"]}"{c.a("pop", 0.1)}/>')
    c.add(f'<g{c.a("fade", 0.25)}>{icon(tile["icon"], 26, 22, T["brand_soft"])}</g>')
    c.add(f'<g{c.a("slide", 0.35)}>{arrow_up_right(Wt - 24 - 13, 26, T["muted"], 13)}</g>')
    c.add(c.text(26, 92, tile["label"].upper(), 11, 600, T["muted"], ls=1.4, anim=("rise", 0.2)))
    value = F.clean(tile["value"])
    size = F.fit(value, 600, Wt - 52, 17, 12)
    c.add(c.text(26, 112, value, size, 600, T["text"], anim=("rise", 0.3)))
    return c


def nice_step(raw: float) -> float:
    if raw <= 0:
        return 1
    mag = 10 ** math.floor(math.log10(raw))
    for k in (1, 2, 2.5, 5, 10):
        if raw <= k * mag:
            return k * mag
    return 10 * mag


def card_activity(base: float, D: dict) -> Card:
    H, pad = 468, 36
    months = D["months"]
    table = "; ".join(f"{m['label']} {m['y']}: {m['count']}" for m in months)
    busiest_d, busiest_n = D["busiest"]
    c = Card("activity", W, H, base, "Contributions per month",
             f"{D['total']} contributions in the last year. Current streak {D['streak']} days, {D['active']} active days, "
             f"busiest day {fmt_date(busiest_d)} with {busiest_n}. Per month: {table}.")
    c.bg(22)
    c.add(c.text(pad, 58, "Contributions per month", 20, 600, T["text"], anim=("rise", 0.05)))
    span = f"{months[0]['label']} {months[0]['y']} - {months[-1]['label']} {months[-1]['y']}"
    c.add(c.text(pad, 80, f"{span}  ·  {D['total']:,} contributions", 13, 400, T["muted"], anim=("rise", 0.1)))
    if D["sample"]:
        sample_tag(c, W - pad, 54)

    # headline numbers
    col_w = (W - 2 * pad) / 3
    stats = [
        ("Current streak", f"{D['streak']:,}", "days",
         (f"Since {fmt_date(D['streak_start'], False)}  ·  longest {D['longest']}" if D["streak"] else f"Longest {D['longest']} days")),
        ("Active days", f"{D['active']:,}", "days", f"{round(100 * D['active'] / len(D['days']))}% of the last {len(D['days'])} days"),
        ("Busiest day", f"{busiest_n:,}", "contributions", fmt_date(busiest_d)),
    ]
    for i, (label, value, unit, sub) in enumerate(stats):
        x = pad + i * col_w + (18 if i else 0)
        d = 0.2 + i * 0.12
        if i:
            c.add(f'<line x1="{n(pad + i * col_w)}" y1="112" x2="{n(pad + i * col_w)}" y2="206" stroke="{T["grid"]}"{c.a("fade", d)}/>')
        c.add(c.text(x, 130, label, 13, 600, T["muted"], anim=("rise", d)))
        c.add(c.text(x, 174, value, 42, 800, T["text"], ls=-0.8, anim=("rise", d + 0.05)))
        vw = F.width(F.clean(value), 800, 42, -0.8)
        c.add(c.text(x + vw + 8, 174, unit, 15, 600, T["text2"], anim=("rise", d + 0.08)))
        c.add(c.text(x, 198, sub, 13, 400, T["muted"], anim=("rise", d + 0.1)))

    # bar chart
    top, bottom = 250, 394
    gx0, gx1 = pad + 40, W - pad
    vals = [m["count"] for m in months]
    vmax = max(vals) or 1
    step = nice_step(vmax / 4)
    ymax = step * math.ceil(vmax / step)
    ticks = [step * k for k in range(int(round(ymax / step)) + 1)]
    for k, tv in enumerate(ticks):
        y = bottom - (bottom - top) * tv / ymax
        c.add(f'<line x1="{gx0}" y1="{n(y)}" x2="{gx1}" y2="{n(y)}" stroke="{T["axis"] if k == 0 else T["grid"]}"{c.a("fade", 0.3)}/>')
        c.add(c.text(gx0 - 10, y + 4, f"{tv:g}" if tv < 10000 else f"{tv / 1000:g}K", 12, 400, T["muted"], "end",
                     anim=("fade", 0.3), style="font-variant-numeric:tabular-nums"))
    slot = (gx1 - gx0) / len(months)
    bw, r = 22, 4
    peak = vals.index(max(vals))
    for i, m in enumerate(months):
        cx = gx0 + slot * (i + 0.5)
        h = (bottom - top) * m["count"] / ymax
        delay = 0.4 + i * 0.06
        if h > 0.5:
            rr = min(r, h)
            x0, y0 = cx - bw / 2, bottom - h
            d = (f"M{n(x0)} {n(bottom)}V{n(y0 + rr)}A{n(rr)} {n(rr)} 0 0 1 {n(x0 + rr)} {n(y0)}H{n(x0 + bw - rr)}"
                 f"A{n(rr)} {n(rr)} 0 0 1 {n(x0 + bw)} {n(y0 + rr)}V{n(bottom)}Z")
            op = ' fill-opacity=".5"' if m["partial"] else ""
            c.add(f'<path d="{d}" fill="{T["brand"]}"{op}{c.a("grow", delay)}/>')
        if i == peak or i == len(months) - 1:  # label only the extreme and the latest month
            c.add(c.text(cx, bottom - h - 9, f"{m['count']:,}", 13, 600, T["text"], "middle", anim=("fade", delay + 0.55)))
        c.add(c.text(cx, bottom + 22, m["label"], 12, 400, T["muted"], "middle", anim=("fade", 0.35)))
    foot = ("Lighter bar: month to date" if months[-1]["partial"] else "")
    if foot:
        c.add(c.text(pad, H - 22, foot, 12, 400, T["muted"], anim=("fade", 1.2)))
    c.add(c.text(W - pad, H - 22, f"Updated {fmt_date(dt.date.fromisoformat(D['generated']))}", 12, 400, T["muted"], "end", anim=("fade", 1.2)))
    return c


def card_repo(base: float, repo: dict) -> Card:
    pad = 36
    title = repo.get("title") or repo["name"]
    slug = f"{repo.get('owner', '')}/{repo['name']}".strip("/")
    tagline = repo.get("tagline") or repo.get("description") or ""
    stack = list(repo.get("stack") or [])
    tag_lines = F.wrap(F.clean(tagline), 400, 16, W - 2 * pad - 20, 2)
    chips_y = 116 + (len(tag_lines) - 1) * 23 + 22
    H = chips_y + 26 + 30
    c = Card(f"repo-{repo['name'].lower()}", W, H, base, f"{title}: {tagline}",
             f"{title} ({slug}). {tagline} Stack: {', '.join(stack)}.")
    c.bg(22)
    c.add(f'<rect x="{pad - 14}" y="34" width="4" height="{H - 68}" rx="2" fill="{T["brand"]}"{c.a("grow", 0.1)}/>')

    # right column: arrow, language, stars, forks (only what we really know)
    right = W - pad
    if repo.get("url"):
        c.add(f'<g{c.a("slide", 0.3)}>{arrow_up_right(right - 13, 40, T["muted"], 13)}</g>')
        right -= 34
    meta_y = 56
    lang = repo.get("language")
    if lang:
        lw = F.width(F.clean(lang), 600, 13)
        c.add(c.text(right, meta_y, lang, 13, 600, T["text2"], "end", anim=("fade", 0.35)))
        c.add(f'<circle cx="{n(right - lw - 12)}" cy="{meta_y - 4.5}" r="5" fill="{LANG_COLORS.get(lang, T["brand_soft"])}"{c.a("pop", 0.35)}/>')
        right -= lw + 12 + 5 + 18
    for key, ic in (("stars", "star"), ("forks", "fork")):
        v = repo.get(key)
        if v:  # hide zero/unknown rather than showing a sad 0
            vs = f"{int(v):,}"
            vw = F.width(vs, 600, 13)
            c.add(c.text(right, meta_y, vs, 13, 600, T["text2"], "end", anim=("fade", 0.4)))
            if ic == "star":
                c.add(f'<g{c.a("pop", 0.4)}>{star_icon(right - vw - 14, meta_y - 4.5, 6.5, T["muted"])}</g>')
            else:
                c.add(f'<g{c.a("pop", 0.4)}>{fork_icon(right - vw - 20, meta_y - 12, T["muted"])}</g>')
            right -= vw + 20 + 18

    # name block
    name_max = right - pad - 20
    tsize = F.fit(F.clean(title), 800, name_max, 24, 18)
    c.add(c.text(pad, 58, title, tsize, 800, T["text"], ls=-0.3, anim=("rise", 0.2)))
    c.add(c.text(pad, 80, slug, 13, 400, T["muted"], anim=("rise", 0.25)))
    # tagline (max two lines)
    for i, line in enumerate(tag_lines):
        c.add(c.text(pad, 116 + i * 23, line, 16, 400, T["text2"], anim=("rise", 0.35 + i * 0.08)))
    # stack chips, as many as fit on one row
    x, y = pad, chips_y
    for i, tech in enumerate(stack):
        tech = F.clean(tech)
        cw = F.width(tech, 600, 12) + 22
        if x + cw > W - pad:
            break
        c.add(f'<g{c.a("pop", 0.55 + i * 0.07)}><rect x="{n(x)}" y="{y}" width="{n(cw)}" height="26" rx="13" fill="{T["raised"]}" stroke="{T["border"]}"/></g>')
        c.add(c.text(x + cw / 2, y + 17, tech, 12, 600, T["text2"], "middle", anim=("fade", 0.6 + i * 0.07)))
        x += cw + 8
    return c


def card_graph(base: float, D: dict) -> Card:
    pad, gutter, gap = 32, 34, 3
    days = D["days"]
    first = days[0][0]
    start = first - dt.timedelta(days=(first.weekday() + 1) % 7)  # Sunday on/before first day
    ncols = (days[-1][0] - start).days // 7 + 1
    pitch = min(15.0, (W - 2 * pad - gutter) / ncols)
    cell = pitch - gap
    gx, gy = pad + gutter, 124
    H = 292

    nz = sorted(cn for _, cn in days if cn > 0)
    q = [nz[min(len(nz) - 1, int(len(nz) * p))] for p in (0.25, 0.5, 0.75)] if nz else [1, 2, 3]

    def level(cn: int) -> int:
        if cn <= 0:
            return 0
        return 1 + sum(cn > t for t in q)

    c = Card("graph", W, H, base, "Contribution graph",
             f"{D['total']} contributions in the last year, one square per day, darker blue is more. "
             f"Busiest day {fmt_date(D['busiest'][0])} with {D['busiest'][1]}.")
    c.bg(22)
    c.add(c.text(pad, 56, "Contribution graph", 20, 600, T["text"], anim=("rise", 0.05)))
    c.add(c.text(pad, 78, f"{D['total']:,} contributions in the last year", 13, 400, T["muted"], anim=("rise", 0.1)))
    if D["sample"]:
        sample_tag(c, W - pad, 54)

    cols: dict[int, list[str]] = defaultdict(list)
    for d, cn in days:
        col, row = (d - start).days // 7, (d.weekday() + 1) % 7
        cols[col].append(f'<rect x="{n(gx + col * pitch)}" y="{n(gy + row * pitch)}" width="{n(cell)}" height="{n(cell)}" '
                         f'rx="3" fill="{RAMP[level(cn)]}"/>')
    for col in sorted(cols):
        c.add(f'<g{c.a("rise6", 0.3 + col * 0.022)}>{"".join(cols[col])}</g>')

    # month labels (skip one that would collide with the previous)
    last_x, seen = -99.0, None
    for col in range(ncols):
        d = start + dt.timedelta(days=col * 7 + 6)  # the Saturday: its month names the column
        if d > days[-1][0]:
            d = days[-1][0]
        if (d.year, d.month) != seen:
            seen = (d.year, d.month)
            x = gx + col * pitch
            if x - last_x >= 34 and x + 24 < W - pad + 20:
                c.add(c.text(x, gy - 10, d.strftime("%b"), 12, 400, T["muted"], anim=("fade", 0.3)))
                last_x = x
    for row, lab in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        c.add(c.text(pad, gy + row * pitch + cell - 2, lab, 11, 400, T["muted"], anim=("fade", 0.3)))

    # legend, right aligned
    ly = gy + 7 * pitch + 28
    x = W - pad
    more_w = F.width("More", 400, 12)
    c.add(c.text(x, ly + 10, "More", 12, 400, T["muted"], "end", anim=("fade", 1.0)))
    x -= more_w + 8
    for lv in range(4, -1, -1):
        x -= cell
        c.add(f'<rect x="{n(x)}" y="{n(ly)}" width="{n(cell)}" height="{n(cell)}" rx="3" fill="{RAMP[lv]}"{c.a("fade", 1.0)}/>')
        x -= gap + 1
    c.add(c.text(x - 3, ly + 10, "Less", 12, 400, T["muted"], "end", anim=("fade", 1.0)))
    c.add(c.text(pad, ly + 10, f"Updated {fmt_date(dt.date.fromisoformat(D['generated']))}", 12, 400, T["muted"], anim=("fade", 1.0)))
    return c


# --------------------------------------------------------------------------- #
# Lint: fail the build if a card uses anything that does not work inside <img>
# --------------------------------------------------------------------------- #
FORBIDDEN = [
    (r"<script", "script"), (r"<foreignObject", "foreignObject"), (r"<iframe", "iframe"),
    (r"<a[\s>]", "link element (links do not work in <img>)"), (r"@import", "@import"),
    (r'href="(?!data:|#)', "external href"), (r"url\(\s*['\"]?(?!data:|#)", "external url()"),
    (r"\son[a-z]+=", "event handler"),
]


def lint(name: str, svg: str) -> None:
    for pat, what in FORBIDDEN:
        if re.search(pat, svg):
            sys.exit(f"{name}: forbidden {what}")
    xml.dom.minidom.parseString(svg.encode("utf-8"))  # well-formed?


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def write_preview(out: Path, files: dict[str, str]) -> None:
    """A page that stacks the cards exactly like README.md does, for screenshots."""
    def img(name, w="100%"):
        return f'<img src="{name}.svg" width="{w}" alt="">'
    rows = [f"<p>{img('hero')}</p>",
            "<p>" + "\n".join(img(f"tile-{t['slug']}", "32.5%") for t in TILES) + "</p>",
            f"<p>{img('activity')}</p>"]
    rows += [f"<p>{img(k)}</p>" for k in files if k.startswith("repo-")]
    rows.append(f"<p>{img('graph')}</p>")
    (out / "preview.html").write_text(
        "<!doctype html><meta charset=utf-8><title>Profile cards preview</title>"
        "<style>body{margin:0;padding:32px 0;background:#0d1117;font:16px/1.5 system-ui}"
        "@media(prefers-color-scheme:light){body{background:#fff}}"
        "main{width:830px;margin:0 auto;text-align:center}p{margin:0 0 16px}img{max-width:100%;vertical-align:middle}</style>"
        "<main>" + "\n".join(rows) + "</main>\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", type=Path, default=ROOT / "data" / "contributions.json")
    ap.add_argument("--repos", type=Path, default=ROOT / "data" / "repos.json")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    out = args.out or ROOT / ("cards-static" if STATIC else "cards")
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.svg"):
        old.unlink()

    D = load_contrib(args.data)
    repos = json.loads(args.repos.read_text(encoding="utf-8"))["repos"]

    # top-to-bottom stagger: each card starts a little after the one above it
    cards: list[Card] = [card_hero(0.0)]
    cards += [card_tile(0.5 + 0.12 * i, t) for i, t in enumerate(TILES)]
    cards.append(card_activity(1.1, D))
    cards += [card_repo(1.8 + 0.3 * i, r) for i, r in enumerate(repos)]
    cards.append(card_graph(1.8 + 0.3 * len(repos) + 0.2, D))

    written: dict[str, str] = {}
    for card in cards:
        svg = card.svg()
        lint(card.name, svg)
        (out / f"{card.name}.svg").write_text(svg, encoding="utf-8")
        written[card.name] = svg
        print(f"{card.name:<28}{len(svg) / 1024:7.1f} KB")
    if STATIC:
        write_preview(out, written)
        print(f"STATIC=1: final frames in {out}/ (open preview.html to screenshot)")


if __name__ == "__main__":
    main()
