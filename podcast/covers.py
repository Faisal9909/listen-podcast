"""Podcast cover art per topic: an icon from Iconify that matches the topic, rendered to PNG.

Apple Podcasts only accepts JPEG or PNG artwork (1400 to 3000 px square), so the SVG design is
rendered with headless Chrome, which also shapes Arabic text correctly.
"""

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

ICONIFY = "https://api.iconify.design"
ICON_SETS = "tabler,lucide,mdi"
FALLBACK_ICON = "tabler:headphones"
SIZE = 1400
# Bump when the design changes so podcast apps fetch the new artwork (they cache by URL).
DESIGN_VERSION = "v1"

STOPWORDS = {
    "a", "an", "and", "the", "of", "for", "to", "in", "on", "with", "from", "how", "what", "why",
    "is", "it", "its", "your", "my", "daily", "part", "series", "episode", "chapter",
}
# Words whose literal icon search is weak, mapped to a better search term.
HINTS = {
    "operating": "hierarchy", "models": "hierarchy", "model": "hierarchy", "servers": "server",
    "networks": "network", "reading": "book", "development": "plant", "lean": "chart-line",
    "sigma": "chart-bar", "pocket": "book", "toolbook": "tool", "finance": "coin",
    "ai": "robot", "strategy": "target", "leadership": "users", "management": "briefcase",
}


def _get(url, timeout=20):
    # Iconify rejects urllib's default user agent.
    req = urllib.request.Request(url, headers={"User-Agent": "listen-podcast-covers/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def _search(term):
    query = urllib.parse.urlencode({"query": term, "limit": 32, "prefixes": ICON_SETS})
    icons = json.loads(_get(f"{ICONIFY}/search?{query}")).get("icons", [])
    # Prefer sets in ICON_SETS order so covers share one drawing style.
    for prefix in ICON_SETS.split(","):
        for icon in icons:
            if icon.startswith(prefix + ":"):
                return icon
    return None


def _search_terms(topic):
    if re.search(r"[؀-ۿ]", topic):
        return ["book-open"]  # Iconify search is English-only
    words = [w for w in re.findall(r"[a-z]+", topic.lower()) if w not in STOPWORDS]
    terms = [HINTS[w] for w in words if w in HINTS]
    terms += sorted(words, key=len, reverse=True)
    return list(dict.fromkeys(terms))


def find_icon(topic):
    """Iconify icon name (prefix:name) for a topic; FALLBACK_ICON when nothing matches."""
    for term in _search_terms(topic):
        try:
            icon = _search(term)
        except OSError:
            break
        if icon:
            return icon
    return FALLBACK_ICON


def icon_svg(icon):
    prefix, name = icon.split(":", 1)
    return _get(f"{ICONIFY}/{prefix}/{name}.svg?color=%23ffffff&width=100%25&height=100%25").decode()


def _palette(topic):
    hue = int(hashlib.sha1(topic.encode()).hexdigest()[:4], 16) % 360
    return f"hsl({hue} 62% 34%)", f"hsl({(hue + 40) % 360} 70% 18%)"


def cover_html(topic, svg):
    start, end = _palette(topic)
    size = 150 if len(topic) <= 16 else 118 if len(topic) <= 28 else 92
    return f"""<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@700&family=Noto+Kufi+Arabic:wght@700&display=block">
<style>
html,body{{margin:0;width:{SIZE}px;height:{SIZE}px;overflow:hidden}}
body{{background:linear-gradient(145deg,{start},{end});color:#fff;font-family:Inter,"Noto Kufi Arabic",sans-serif;
display:flex;flex-direction:column;align-items:center;justify-content:center;gap:90px}}
.icon{{width:520px;height:520px;opacity:.95}}
.icon svg{{width:100%;height:100%}}
h1{{margin:0 110px;font-size:{size}px;line-height:1.15;text-align:center;letter-spacing:-.01em}}
.tag{{position:absolute;bottom:70px;font-size:44px;opacity:.65;letter-spacing:.3em}}
</style></head><body>
<div class="icon">{svg}</div>
<h1 dir="auto">{html.escape(topic)}</h1>
<div class="tag">LISTEN</div>
</body></html>"""


def _chrome():
    for name in (os.environ.get("CHROME_BIN"), "google-chrome", "chromium", "chromium-browser", "chrome"):
        if name and shutil.which(name):
            return shutil.which(name)
    return None


def render_png(page_html, out):
    chrome = _chrome()
    if not chrome:
        raise RuntimeError("no Chrome/Chromium found")
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "cover.html"
        page.write_text(page_html, encoding="utf-8")
        subprocess.run(
            [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
             f"--window-size={SIZE},{SIZE}", "--virtual-time-budget=8000",
             f"--screenshot={out}", page.as_uri()],
            check=True, capture_output=True, timeout=90,
        )
    if not Path(out).exists() or Path(out).stat().st_size == 0:
        raise RuntimeError("Chrome produced no image")


def make_cover(topic, out):
    """Write a PNG cover for the topic to out. Returns the icon used."""
    icon = find_icon(topic)
    render_png(cover_html(topic, icon_svg(icon)), out)
    return icon
