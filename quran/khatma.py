"""Reading and review feeds for one reciter.

  <key>.xml         the reading feed people already follow, first hizb to last. "new" gives its 60
                    episodes fresh ids (and the khatma number), so Apple Podcasts shows them all as
                    new and unplayed and the listener starts again from the first hizb.
  <key>-review.xml  review: the last hizbs only (REVIEW_FROM to 60); a separate show, so reviewing
                    never moves the reading position.

usage: python quran/khatma.py <key>          rebuild the review feed
       python quran/khatma.py <key> new      start the next khatma in the reading feed
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

HERE = Path(__file__).parent
REVIEW_FROM = 41
ORDINALS = ["الأولى", "الثانية", "الثالثة", "الرابعة", "الخامسة", "السادسة", "السابعة", "الثامنة",
            "التاسعة", "العاشرة"]


def ordinal(n):
    return f"الختمة {ORDINALS[n - 1]}" if n <= len(ORDINALS) else f"الختمة {n}"


def main():
    key = sys.argv[1]
    state_path = HERE / "khatma.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    entry = state.setdefault(key, {"khatma": 1, "started": datetime.now(timezone.utc).isoformat()})
    if sys.argv[2:] == ["new"]:
        entry.update(khatma=entry["khatma"] + 1, started=datetime.now(timezone.utc).isoformat())
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    source = (HERE / f"{key}.xml").read_text(encoding="utf-8")
    head, rest = source.split("<item>", 1)
    items = ["<item>" + part for part in rest.split("<item>")]
    items[-1] = items[-1].split("</channel>")[0]
    name = re.search(r"<title>[^<]*: ([^<]+)</title>", head).group(1)
    started = datetime.fromisoformat(entry["started"])

    def feed(title, description, chosen, guid_prefix):
        top = re.sub(r"<title>[^<]*</title>", f"<title>{title}</title>", head, count=1)
        top = re.sub(r"<description>[^<]*</description>", f"<description>{description}</description>", top, count=1)
        out = []
        for n, item in chosen:
            item = re.sub(r"<guid isPermaLink=\"false\">[^<]*</guid>",
                          f'<guid isPermaLink="false">{guid_prefix}-{n:02d}</guid>', item)
            # one minute apart, all in the past, so the order is the hizb order
            date = started - timedelta(minutes=61 - n)
            item = re.sub(r"<pubDate>[^<]*</pubDate>", f"<pubDate>{format_datetime(date, usegmt=True)}</pubDate>", item)
            out.append(item.rstrip() + "\n")
        return top + "".join(out) + "</channel>\n</rss>\n"

    numbered = list(enumerate(items, 1))
    k = entry["khatma"]
    if sys.argv[2:] == ["new"]:
        # the reading feed keeps its title and description; only the ids, dates and khatma change
        top = re.sub(r"<description>[^<]*</description>",
                     f"<description>القرآن الكريم بالأحزاب بصوت {name}: ستون حلقة، حلقة لكل حزب. {ordinal(k)}. "
                     "المصدر: mp3quran.net</description>", head, count=1)
        body = feed(name, "", numbered, f"{key}-k{k}-hizb").split("<item>", 1)[1]
        (HERE / f"{key}.xml").write_text(top + "<item>" + body, encoding="utf-8")
    (HERE / f"{key}-review.xml").write_text(feed(
        f"{name} — المراجعة",
        f"مراجعة الأحزاب من {REVIEW_FROM} إلى 60 بصوت {name}.",
        numbered[REVIEW_FROM - 1:], f"{key}-review-hizb"), encoding="utf-8")
    print(f"{key}: {ordinal(k)}, review {61 - REVIEW_FROM} episodes")


main()
