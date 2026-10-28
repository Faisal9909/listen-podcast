"""Turn new Gmail messages into podcast episodes.

Runs inside GitHub Actions. Each run:
  1. reads the episode list already published on GitHub Pages,
  2. finds new Gmail messages matching GMAIL_QUERY,
  3. converts each one to MP3 with Microsoft Edge neural voices (edge-tts),
  4. joins finished series ("Part 1", "Part 2", ...) into one episode once no new part arrived
     for COMBINE_AFTER_HOURS,
  5. rebuilds the site (feed.xml, one feed per topic under topics/, player page) into _site/.

Nothing is deployed when there are no new messages, so hourly runs are cheap.
The episodes live under a secret path (FEED_TOKEN) that never appears in the repo.
"""

import asyncio
import base64
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
VOICE_EN = os.environ.get("VOICE_EN") or "en-US-AndrewNeural"
RATE = os.environ.get("TTS_RATE") or "+0%"
MAX_EPISODES = int(os.environ.get("MAX_EPISODES") or 60)
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN") or 1)
PODCAST_TITLE = os.environ.get("PODCAST_TITLE") or "استمع — نصائحي اليومية"

GOOGLE_TTS_KEY = os.environ.get("GOOGLE_TTS_KEY", "").strip()
GOOGLE_TTS_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"


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
    text = text.replace("ﷺ", "صلى الله عليه وسلم")  # the ligature is often skipped by TTS voices
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
    return f"to:{local}+listen@{domain} newer_than:30d"


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

    conn = imaplib.IMAP4_SSL("imap.gmail.com", timeout=60)
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
        "date": sent.astimezone(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------- speech


CHUNK_CHARS = 2000
CHUNK_TIMEOUT_SECONDS = 240
PARALLEL_CHUNKS = 4


def split_chunks(text, limit=CHUNK_CHARS):
    """Split at paragraph/sentence boundaries so each request stays small."""
    pieces = re.split(r"(?<=[.!?؟\n])\s+", text)
    chunks, current = [], ""
    for piece in pieces:
        while len(piece) > limit:
            cut = piece.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(piece[:cut])
            piece = piece[cut:].lstrip()
        if current and len(current) + len(piece) + 1 > limit:
            chunks.append(current)
            current = piece
        else:
            current = f"{current} {piece}".strip()
    if current:
        chunks.append(current)
    return chunks


def _google_chunk(text, voice):
    """voice looks like "google:en-US-Chirp3-HD-Charon"."""
    if not GOOGLE_TTS_KEY:
        raise RuntimeError("GOOGLE_TTS_KEY is not set")
    name = voice.split(":", 1)[1]
    body = json.dumps({
        "input": {"text": text},
        "voice": {"languageCode": "-".join(name.split("-")[:2]), "name": name},
        "audioConfig": {"audioEncoding": "MP3", "sampleRateHertz": 24000},
    }).encode()
    req = urllib.request.Request(
        f"{GOOGLE_TTS_URL}?key={GOOGLE_TTS_KEY}", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=CHUNK_TIMEOUT_SECONDS) as res:
            return base64.b64decode(json.load(res)["audioContent"])
    except urllib.error.HTTPError as exc:
        # Google's error body names the problem (API disabled, bad voice, billing) and holds no secrets.
        raise RuntimeError(f"Google TTS {exc.code}: {exc.read()[:300].decode(errors='replace')}") from None


async def _synthesize_chunk(text, voice):
    if voice.startswith("google:"):
        return await asyncio.to_thread(_google_chunk, text, voice)

    import edge_tts

    audio = bytearray()
    async for event in edge_tts.Communicate(text, voice, rate=RATE).stream():
        if event["type"] == "audio":
            audio.extend(event["data"])
    if not audio:
        raise RuntimeError("empty audio")
    return bytes(audio)


async def _synthesize_all(chunks, voice):
    gate = asyncio.Semaphore(PARALLEL_CHUNKS)

    async def one(index, chunk):
        async with gate:
            for attempt in range(1, 4):
                try:
                    data = await asyncio.wait_for(_synthesize_chunk(chunk, voice), CHUNK_TIMEOUT_SECONDS)
                    log(f"  chunk {index}/{len(chunks)} done")
                    return data
                except Exception as exc:  # noqa: BLE001 - edge-tts raises several types
                    log(f"  chunk {index}/{len(chunks)} attempt {attempt} failed: {type(exc).__name__} {exc}"[:400])
                    await asyncio.sleep(5 * attempt)
            raise RuntimeError("TTS failed after 3 attempts")

    return await asyncio.gather(*(one(i, c) for i, c in enumerate(chunks, 1)))


def synthesize(text, path, voice):
    parts = asyncio.run(_synthesize_all(split_chunks(text), voice))
    path.write_bytes(b"".join(parts))


# ---------------------------------------------------------------- topics and series

COMBINE_AFTER_HOURS = float(os.environ.get("COMBINE_AFTER_HOURS") or 6)
# "Topic, Part 3: ...", "Topic — Chapter 8, Episode 1: ...", "Topic – Title", "Topic: Title"
TOPIC_SPLIT = re.compile(r",\s*(?:Part|Chapter|Episode|الجزء|الحلقة)\b|\s[-–—|]\s|:\s", re.I)
# "Series, Part 3", "Series — Chapter 8, Episode 1", "السلسلة - الجزء 3"
PART_NUMBER = re.compile(r"^(.*?)(?:,\s*|\s+[-–—:]?\s*)(?:Part|Episode|الجزء|الحلقة)\s*(\d+)\b", re.I)


def _title_topic(title):
    tag = re.match(r"^\[([^\]]+)\]", title)
    if tag:
        return tag.group(1).strip()
    head = TOPIC_SPLIT.split(title, 1)
    return head[0].strip() if len(head) > 1 and head[0].strip() else None


def topic_of(ep):
    """Each topic gets its own feed: a leading [Tag], else the title text before the first separator."""
    series = ep.get("series")
    return _title_topic(series or ep["title"]) or series or "متفرقات"


def part_of(ep):
    """(series, part number) for an episode, or (None, None)."""
    if ep.get("series"):
        return ep["series"], None
    match = PART_NUMBER.match(ep["title"])
    return (match.group(1).strip(), int(match.group(2))) if match else (None, None)


def plan_combines(published, pending):
    """Series whose parts 1..N are all published and quiet for COMBINE_AFTER_HOURS become one episode.

    A later part is appended to the existing combined episode. Series with emails still waiting
    to be converted are left alone until those are published.
    """
    waiting = {part_of(m)[0] for m in pending}
    now = datetime.now(timezone.utc)
    groups = {}
    for ep in published:
        series, number = part_of(ep)
        if series and series not in waiting:
            groups.setdefault(series, {"full": None, "parts": []})
            if number is None:
                groups[series]["full"] = ep
            else:
                groups[series]["parts"].append((number, ep))
    plans = []
    for series, group in groups.items():
        full, parts = group["full"], sorted(group["parts"], key=lambda p: p[0])
        done = full["parts"] if full else []
        numbers = done + [n for n, _ in parts]
        if not parts or len(numbers) < 2 or sorted(numbers) != list(range(1, len(numbers) + 1)):
            continue
        if min(n for n, _ in parts) <= max(done, default=0):
            continue  # a part number that is already inside the combined episode
        latest = max(datetime.fromisoformat(ep["date"]) for _, ep in parts)
        if (now - latest).total_seconds() < COMBINE_AFTER_HOURS * 3600:
            continue
        plans.append({"series": series, "full": full, "parts": parts})
    return plans


def combined_episode(plan, base_url, ep_dir):
    """Build the combined MP3 from the live site's files; returns (episode, replaced ids) or None."""
    pieces = ([plan["full"]] if plan["full"] else []) + [ep for _, ep in plan["parts"]]
    audio = []
    for ep in pieces:
        data = http_get(f"{base_url}/episodes/{ep['id']}.mp3")
        if not data:
            return None
        audio.append(data)
    ids = [ep["id"] for ep in pieces]
    numbers = (plan["full"]["parts"] if plan["full"] else []) + [n for n, _ in plan["parts"]]
    series = plan["series"]
    full_id = "full-" + hashlib.sha1(",".join(ids).encode()).hexdigest()[:16]
    mp3 = ep_dir / f"{full_id}.mp3"
    mp3.write_bytes(b"".join(audio))
    label = "كاملة" if is_arabic(series) else "Full"
    texts = [plan["full"]["text"]] if plan["full"] else []
    texts += [f"{ep['title']}\n\n{ep['text']}" for _, ep in plan["parts"]]
    episode = {
        "id": full_id,
        "title": f"{series} ({label}, {len(numbers)})",
        "text": "\n\n".join(texts),
        "date": datetime.now(timezone.utc).isoformat(),
        "series": series,
        "parts": numbers,
        "bytes": mp3.stat().st_size,
        "duration": mp3_seconds(mp3),
    }
    return episode, ids


# ---------------------------------------------------------------- site


def http_get(url):
    try:
        with urllib.request.urlopen(url, timeout=60) as res:
            return res.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def load_removed(base_url):
    data = http_get(f"{base_url}/removed.json?t={int(time.time())}")
    return json.loads(data) if data else []


def load_published(base_url):
    data = http_get(f"{base_url}/episodes.json?t={int(time.time())}")
    return json.loads(data) if data else []


def topic_slug(topic):
    return hashlib.sha1(topic.encode()).hexdigest()[:10]


def write_feed(episodes, base_url, path, title=PODCAST_TITLE):
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
    <title>{escape(title)}</title>
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
    topics = {}
    for ep in episodes:
        topics.setdefault(topic_of(ep), 0)
        topics[topic_of(ep)] += 1
    links = [
        f'<li><a href="podcast://{feed_host}/topics/{topic_slug(t)}.xml">{html.escape(t)}</a> <span>{n}</span></li>'
        for t, n in topics.items()
    ]
    page = page.replace("{{TOPICS}}", "\n".join(links))
    page = page.replace("{{EPISODES}}", "\n".join(rows) or "<p>لا توجد حلقات بعد.</p>")
    path.write_text(page, encoding="utf-8")


def mp3_seconds(path):
    from mutagen.mp3 import MP3, HeaderNotFoundError

    try:
        return round(MP3(path).info.length)
    except HeaderNotFoundError:
        return round(path.stat().st_size / 6000)  # edge-tts default: 48 kbps


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
    removed = load_removed(base_url)
    # REMOVE_MATCH drops episodes whose title contains it; their ids are remembered so the
    # matching emails are never converted again.
    match = os.environ.get("REMOVE_MATCH", "").strip()
    dropped = [ep for ep in published if match and match in ep["title"]]
    if dropped:
        removed += [ep["id"] for ep in dropped]
        published = [ep for ep in published if ep not in dropped]
    known = {ep["id"] for ep in published} | set(removed)
    log(f"Published episodes: {len(published)}, removed now: {len(dropped)}")

    candidates = fetch_messages()
    test_text = os.environ.get("TEST_TEXT", "").strip()
    if test_text:
        # SAMPLE_VOICES="a,b,c" turns the test text into one sample episode per voice.
        voices = [v.strip() for v in os.environ.get("SAMPLE_VOICES", "").split(",") if v.strip()]
        now = datetime.now(timezone.utc)
        for n, voice in enumerate(voices or [None]):
            stamp = now.replace(microsecond=n)
            candidates.append({
                "id": f"test-{now:%Y%m%d%H%M%S}-{n}",
                "title": f"عينة صوت: {voice.split(':')[-1]}" if voice else f"حلقة تجريبية {now:%Y-%m-%d %H:%M}",
                "text": clean_for_speech(test_text),
                "date": stamp.isoformat(),
                "voice": voice,
            })
    new = sorted((m for m in candidates if m["id"] not in known), key=lambda m: datetime.fromisoformat(m["date"]))
    # Publish a few at a time so the first episodes appear quickly; the workflow
    # starts another run while more are waiting.
    # Test samples are not stored anywhere else, so they always go in this run.
    tests = [m for m in new if m["id"].startswith("test-")]
    emails = [m for m in new if not m["id"].startswith("test-")]
    waiting = len(emails) - MAX_NEW_PER_RUN
    new = tests + emails[:MAX_NEW_PER_RUN]
    log(f"New episodes this run: {len(new)}, still waiting: {max(0, waiting)}")
    combines = plan_combines(published, emails)
    log(f"Series ready to combine: {len(combines)}")
    if not new and not dropped and not combines:
        set_output("changed", "false")
        return

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    ep_dir = OUT_DIR / token / "episodes"
    ep_dir.mkdir(parents=True)

    created = []
    for msg in new:
        mp3 = ep_dir / f"{msg['id']}.mp3"
        spoken = f"{msg['title']}.\n\n{msg['text']}"
        try:
            voice = msg.pop("voice", None) or (VOICE_AR if is_arabic(msg["text"]) else VOICE_EN)
            synthesize(spoken, mp3, voice)
        except RuntimeError as exc:
            log(f"Skipping one message, will retry next run: {exc}")
            continue
        created.append({**msg, "bytes": mp3.stat().st_size, "duration": mp3_seconds(mp3)})
    # The parts are replaced by the combined episode and remembered as removed, so they never come back.
    for plan in combines:
        result = combined_episode(plan, base_url, ep_dir)
        if not result:
            log("Skipping one series, a part could not be downloaded")
            continue
        episode, replaced = result
        created.append(episode)
        removed += replaced
        published = [ep for ep in published if ep["id"] not in replaced]
    if not created and not dropped:
        set_output("changed", "false")
        return

    episodes = sorted(published + created, key=lambda e: datetime.fromisoformat(e["date"]), reverse=True)[:MAX_EPISODES]
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
    (site / "removed.json").write_text(json.dumps(removed), encoding="utf-8")
    write_feed(episodes, base_url, site / "feed.xml")
    (site / "topics").mkdir()
    for topic in {topic_of(ep) for ep in episodes}:
        mine = [ep for ep in episodes if topic_of(ep) == topic]
        write_feed(mine, base_url, site / "topics" / f"{topic_slug(topic)}.xml", topic)
    write_player(episodes, base_url, site / "index.html")
    shutil.copy(HERE / "cover.png", site / "cover.png")
    (OUT_DIR / "index.html").write_text("<!doctype html><title>Listen</title>", encoding="utf-8")
    (OUT_DIR / ".nojekyll").write_text("", encoding="utf-8")

    log(f"Site built with {len(episodes)} episode(s).")
    set_output("changed", "true")
    set_output("more", "true" if waiting > 0 and created else "false")


if __name__ == "__main__":
    main()
