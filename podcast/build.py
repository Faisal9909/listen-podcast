"""Turn new Gmail messages into podcast episodes.

Runs inside GitHub Actions. Each run:
  1. reads the episode list already published on GitHub Pages,
  2. finds new Gmail messages matching GMAIL_QUERY,
  3. converts each one to MP3 with Microsoft Edge neural voices (edge-tts),
  4. rebuilds the site (feed.xml + player page) into _site/ for deployment.

Nothing is deployed when there are no new messages, so hourly runs are cheap.
The episodes live under a secret path (FEED_TOKEN) that never appears in the repo.
"""

import asyncio
import email
import email.policy
import email.utils
import hashlib
import html
import imaplib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
OUT_DIR = Path(os.environ.get("OUT_DIR", "_site"))

VOICE_AR = os.environ.get("VOICE_AR") or "ar-SA-HamedNeural"
VOICE_EN = os.environ.get("VOICE_EN") or "en-US-AndrewMultilingualNeural"
RATE = os.environ.get("TTS_RATE") or "+0%"
MAX_EPISODES = int(os.environ.get("MAX_EPISODES") or 60)
PODCAST_TITLE = os.environ.get("PODCAST_TITLE") or "استمع — نصائحي اليومية"

# edge-tts output format: audio-24khz-48kbitrate-mono-mp3
MP3_BYTES_PER_SECOND = 48000 / 8


def log(msg):
    # Public repo: workflow logs are public, so never print email content here.
    print(msg, flush=True)


# ---------------------------------------------------------------- text cleanup


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"}
    SKIP = {"style", "script", "head", "title"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


def html_to_text(markup):
    parser = _TextExtractor()
    parser.feed(markup)
    return html.unescape("".join(parser.parts))


FORWARD_MARKER = re.compile(r"^-{3,}.*(forwarded message|رسالة مُعاد توجيهها|رسالة معاد توجيهها).*-{3,}$", re.I)
FORWARD_HEADER = re.compile(r"^(from|date|sent|subject|to|cc|من|التاريخ|الموضوع|إلى|نسخة)\s*:", re.I)
SUBJECT_PREFIX = re.compile(r"^\s*((fwd?|re|إعادة توجيه|رد)\s*:\s*)+", re.I)


def clean_subject(subject):
    return SUBJECT_PREFIX.sub("", subject).strip()


def clean_for_speech(text):
    """Strip things a listener should not hear: links, markdown symbols, footers."""
    text = text.replace("\r\n", "\n")
    lines = []
    in_forward_header = False
    for line in text.split("\n"):
        line = line.strip()
        if FORWARD_MARKER.match(line):
            in_forward_header = True
            continue
        if in_forward_header:
            if not line or FORWARD_HEADER.match(line):
                in_forward_header = bool(line)
                continue
            in_forward_header = False
        if re.match(r"^(>|--\s*$|unsubscribe|إلغاء الاشتراك|sent from my)", line, re.I):
            if line.startswith("--"):
                break  # signature block
            continue
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)  # [label](url) -> label
        line = re.sub(r"https?://\S+|www\.\S+", "", line)
        line = re.sub(r"^#{1,6}\s*", "", line)  # markdown headings
        line = re.sub(r"^[-*•]\s+", "", line)  # bullets
        line = re.sub(r"[*_`~|]{1,3}", "", line)  # emphasis / table pipes
        line = re.sub(r"\s{2,}", " ", line).strip()
        lines.append(line)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def is_arabic(text):
    letters = re.findall(r"[^\W\d_]", text)
    if not letters:
        return True
    arabic = sum(1 for ch in letters if "؀" <= ch <= "ۿ")
    return arabic / len(letters) > 0.3


# ---------------------------------------------------------------- gmail


def gmail_query():
    query = os.environ.get("GMAIL_QUERY", "").strip()
    if query:
        return query
    local, _, domain = os.environ["GMAIL_ADDRESS"].partition("@")
    return f"deliveredto:{local}+listen@{domain} newer_than:30d"


def _all_mail_folder(conn):
    # The folder name is localized ("[Gmail]/All Mail", "[Gmail]/كل البريد", ...); find it by flag.
    _, folders = conn.list()
    for raw in folders or []:
        line = raw.decode(errors="replace")
        if "\\All" in line:
            match = re.search(r'"([^"]+)"\s*$', line) or re.search(r"(\S+)\s*$", line)
            return '"' + match.group(1) + '"'
    return "INBOX"


def fetch_messages():
    address = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not address or not password:
        log("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set; skipping Gmail.")
        return []

    conn = imaplib.IMAP4_SSL("imap.gmail.com")
    conn.login(address, password.replace(" ", ""))
    try:
        conn.select(_all_mail_folder(conn), readonly=True)
        query = gmail_query().replace("\\", "\\\\").replace('"', '\\"')
        status, data = conn.uid("SEARCH", "X-GM-RAW", f'"{query}"')
        if status != "OK":
            raise RuntimeError(f"Gmail search failed: {status}")
        uids = data[0].split() if data and data[0] else []
        log(f"Gmail: {len(uids)} matching message(s).")

        messages = []
        for uid in uids:
            status, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if status != "OK" or raw is None:
                continue
            messages.append(parse_message(raw, fallback_id=uid.decode()))
        return [m for m in messages if m["text"]]
    finally:
        conn.logout()


def parse_message(raw, fallback_id=""):
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        content = body.get_content()
        text = html_to_text(content) if body.get_content_type() == "text/html" else content

    date = msg.get("Date")
    try:
        sent = email.utils.parsedate_to_datetime(date) if date else None
    except (TypeError, ValueError):
        sent = None
    sent = sent or datetime.now(timezone.utc)
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)

    message_id = (msg.get("Message-ID") or fallback_id).strip()
    return {
        "id": hashlib.sha1(message_id.encode()).hexdigest()[:16],
        "title": clean_subject(str(msg.get("Subject") or "")) or "بدون عنوان",
        "text": clean_for_speech(text),
        "date": sent.isoformat(),
    }


# ---------------------------------------------------------------- speech


async def _synthesize(text, voice, path):
    import edge_tts

    await edge_tts.Communicate(text, voice, rate=RATE).save(str(path))


def synthesize(text, path, voice):
    for attempt in range(1, 4):
        try:
            asyncio.run(_synthesize(text, voice, path))
            if path.exists() and path.stat().st_size > 0:
                return
            raise RuntimeError("empty audio")
        except Exception as exc:  # noqa: BLE001 - edge-tts raises several types
            log(f"TTS attempt {attempt} failed: {type(exc).__name__}")
            time.sleep(5 * attempt)
    raise RuntimeError("TTS failed after 3 attempts")


# ---------------------------------------------------------------- site


def http_get(url):
    try:
        with urllib.request.urlopen(url, timeout=60) as res:
            return res.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def load_published(base_url):
    data = http_get(f"{base_url}/episodes.json?t={int(time.time())}")
    return json.loads(data) if data else []


def write_feed(episodes, base_url, path):
    items = []
    for ep in episodes:
        pub = email.utils.format_datetime(datetime.fromisoformat(ep["date"]))
        items.append(
            f"""    <item>
      <title>{escape(ep["title"])}</title>
      <guid isPermaLink="false">{ep["id"]}</guid>
      <pubDate>{pub}</pubDate>
      <enclosure url="{base_url}/episodes/{ep["id"]}.mp3" length="{ep["bytes"]}" type="audio/mpeg"/>
      <itunes:duration>{ep["duration"]}</itunes:duration>
      <description>{escape(ep["text"])}</description>
    </item>"""
        )
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{escape(PODCAST_TITLE)}</title>
    <link>{base_url}/</link>
    <language>ar</language>
    <description>رسائل البريد محوّلة إلى صوت تلقائيًا.</description>
    <itunes:author>Listen</itunes:author>
    <itunes:image href="{base_url}/cover.png"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:block>Yes</itunes:block>
{chr(10).join(items)}
  </channel>
</rss>
"""
    path.write_text(feed, encoding="utf-8")


def write_player(episodes, base_url, path):
    feed_host = base_url.split("://", 1)[1]
    rows = []
    for ep in episodes:
        day = datetime.fromisoformat(ep["date"]).strftime("%Y-%m-%d")
        minutes = max(1, round(ep["duration"] / 60))
        rows.append(
            f"""<article>
  <h2>{html.escape(ep["title"])}</h2>
  <p class="meta">{day} · {minutes} د</p>
  <audio controls preload="none" src="episodes/{ep["id"]}.mp3"></audio>
  <details><summary>النص</summary><p>{html.escape(ep["text"]).replace(chr(10), "<br>")}</p></details>
</article>"""
        )
    page = (HERE / "player.html").read_text(encoding="utf-8")
    page = page.replace("{{TITLE}}", html.escape(PODCAST_TITLE))
    page = page.replace("{{FEED_HOST}}", html.escape(feed_host))
    page = page.replace("{{FEED_URL}}", html.escape(f"{base_url}/feed.xml"))
    page = page.replace("{{EPISODES}}", "\n".join(rows) or "<p>لا توجد حلقات بعد.</p>")
    path.write_text(page, encoding="utf-8")


def set_output(name, value):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def main():
    token = os.environ.get("FEED_TOKEN", "").strip()
    site_url = os.environ.get("SITE_URL", "").rstrip("/")
    if len(token) < 16 or not site_url:
        sys.exit("FEED_TOKEN (16+ chars) and SITE_URL are required.")
    base_url = f"{site_url}/{token}"

    published = load_published(base_url)
    known = {ep["id"] for ep in published}
    log(f"Published episodes: {len(published)}")

    candidates = fetch_messages()
    test_text = os.environ.get("TEST_TEXT", "").strip()
    if test_text:
        now = datetime.now(timezone.utc)
        candidates.append({
            "id": "test-" + now.strftime("%Y%m%d%H%M%S"),
            "title": "حلقة تجريبية " + now.strftime("%Y-%m-%d %H:%M"),
            "text": clean_for_speech(test_text),
            "date": now.isoformat(),
        })
    new = [m for m in candidates if m["id"] not in known]
    log(f"New episodes: {len(new)}")
    if not new:
        set_output("changed", "false")
        return

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    ep_dir = OUT_DIR / token / "episodes"
    ep_dir.mkdir(parents=True)

    created = []
    for msg in sorted(new, key=lambda m: m["date"]):
        mp3 = ep_dir / f"{msg['id']}.mp3"
        spoken = f"{msg['title']}.\n\n{msg['text']}"
        try:
            synthesize(spoken, mp3, VOICE_AR if is_arabic(msg["text"]) else VOICE_EN)
        except RuntimeError as exc:
            log(f"Skipping one message, will retry next run: {exc}")
            continue
        size = mp3.stat().st_size
        created.append({**msg, "bytes": size, "duration": round(size / MP3_BYTES_PER_SECOND)})
    if not created:
        set_output("changed", "false")
        return

    episodes = sorted(published + created, key=lambda e: e["date"], reverse=True)[:MAX_EPISODES]
    # Pages deploys replace the whole site, so carry the older MP3s over from the live site.
    for ep in episodes:
        mp3 = ep_dir / f"{ep['id']}.mp3"
        if not mp3.exists():
            data = http_get(f"{base_url}/episodes/{ep['id']}.mp3")
            if data:
                mp3.write_bytes(data)
    episodes = [ep for ep in episodes if (ep_dir / f"{ep['id']}.mp3").exists()]

    site = OUT_DIR / token
    (site / "episodes.json").write_text(json.dumps(episodes, ensure_ascii=False, indent=1), encoding="utf-8")
    write_feed(episodes, base_url, site / "feed.xml")
    write_player(episodes, base_url, site / "index.html")
    shutil.copy(HERE / "cover.png", site / "cover.png")
    (OUT_DIR / "index.html").write_text("<!doctype html><title>Listen</title>", encoding="utf-8")
    (OUT_DIR / ".nojekyll").write_text("", encoding="utf-8")

    log(f"Site built with {len(episodes)} episode(s).")
    set_output("changed", "true")


if __name__ == "__main__":
    main()
