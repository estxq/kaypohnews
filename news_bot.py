#!/usr/bin/env python3
"""
Kaypoh News bot
---------------
Each run posts one Finance story (from Business Times) and one General story
(from CNA / Straits Times) to a Telegram channel, each summarised by OpenAI.

Secrets are read from environment variables (set these as GitHub Secrets
when you deploy, never hard-code them):
    OPENAI_API_KEY       - your OpenAI API key
    TELEGRAM_BOT_TOKEN   - your bot token from BotFather
    TELEGRAM_CHANNEL     - your channel, e.g. @kaypohnews

To turn a story into a LinkedIn opinion, she forwards the post to the
separate always-on assistant.py bot in their private chat, which picks up
the forward and helps her draft it. This script just posts and exits, so it
keeps running fine on GitHub Actions.
"""

import os
import json
import html
import time
import urllib.parse
import urllib.request

import feedparser
from openai import OpenAI

# ----------------------------------------------------------------------
# 1. CONFIG  -  this is the only part you normally edit
# ----------------------------------------------------------------------

FINANCE_FEEDS = [
    "https://www.businesstimes.com.sg/rss/top-stories",   # Business Times
]

GENERAL_FEEDS = [
    "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml",  # CNA top
    "https://www.straitstimes.com/news/singapore/rss.xml",                    # ST Singapore
]

MODEL = "gpt-4.1-mini"
SEEN_FILE = "seen.json"

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL = os.environ["TELEGRAM_CHANNEL"]

# ----------------------------------------------------------------------
# 2. REMEMBER WHAT WE'VE SEEN
# ----------------------------------------------------------------------

def load_seen():
    try:
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen)[-1000:], f)

# ----------------------------------------------------------------------
# 3. PULL NEW ARTICLES FROM RSS
# ----------------------------------------------------------------------

def fetch_candidates(feeds, seen, limit=6):
    out = []
    for url in feeds:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            link = entry.get("link")
            if not link or link in seen:
                continue
            out.append({
                "title": entry.get("title", "").strip(),
                "summary": entry.get("summary", "").strip()[:400],
                "link": link,
            })
            if len(out) >= limit:
                return out
    return out

def fetch_top_new(feeds, seen):
    found = fetch_candidates(feeds, seen, limit=1)
    return found[0] if found else None

# ----------------------------------------------------------------------
# 4. ASK OPENAI TO SUMMARISE + CLASSIFY
# ----------------------------------------------------------------------

def check_and_summarise_finance(article):
    client = OpenAI(api_key=OPENAI_API_KEY)
    prompt = f"""You are the editor of "Kaypoh News", a Singapore news digest
read by an insurance & financial advisor who posts opinions on LinkedIn.

Decide if this story is useful material for her. It counts if it relates to ANY of:
- Insurance & protection
- CPF, MediShield, Integrated Shield, retirement, or healthcare costs
- Personal finance & financial planning (savings, loans, household money)
- Scams, fraud & financial literacy
- Markets, investing & the broader economy
- Money-related human-interest or life events (cost of living, aging, etc.)

Reject ONLY stories with no money/finance/protection angle at all
(e.g. pure sports results, celebrity gossip, entertainment).

Article title: {article['title']}
Article blurb: {article['summary']}

If it fits, write ONE clear sentence summarising it.
Respond with ONLY valid JSON: {{"fits": true/false, "summary": "..."}}"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)

def summarise_general(article):
    client = OpenAI(api_key=OPENAI_API_KEY)
    prompt = f"""You are the editor of "Kaypoh News", a Singapore news digest.

Article title: {article['title']}
Article blurb: {article['summary']}

Write ONE clear sentence summarising this story for a general reader.
Respond with ONLY valid JSON: {{"summary": "..."}}"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)["summary"]

# ----------------------------------------------------------------------
# 5. POST TO TELEGRAM
# ----------------------------------------------------------------------

def format_post(category, summary):
    label = "💰 <b>Finance</b>" if category == "finance" else "📰 <b>General</b>"
    return f'{label}\n{html.escape(summary)}'

def post_to_telegram(text, url):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    link_preview = json.dumps({
        "url": url,
        "prefer_large_media": True,
        "show_above_text": False,
    })

    # No button needed: she forwards whichever post interests her straight
    # to the assistant bot's private chat, and it picks up the forward there.
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHANNEL,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": link_preview,
    }).encode()

    req = urllib.request.Request(api, data=data)
    with urllib.request.urlopen(req) as resp:
        result = json.load(resp)
        if not result.get("ok"):
            raise RuntimeError(f"Telegram error: {result}")

# ----------------------------------------------------------------------
# 6. MAIN
# ----------------------------------------------------------------------

def main():
    seen = load_seen()
    posted = 0

    finance_candidates = fetch_candidates(FINANCE_FEEDS, seen, limit=6)
    for article in finance_candidates:
        print(f"Checking finance: {article['title']}")
        result = check_and_summarise_finance(article)
        seen.add(article["link"])
        if result.get("fits"):
            text = format_post("finance", result["summary"])
            post_to_telegram(text, article["link"])
            posted += 1
            time.sleep(1)
            break
        else:
            print("  ...skipped (not a finance topic)")

    general = fetch_top_new(GENERAL_FEEDS, seen)
    if general:
        print(f"Summarising general: {general['title']}")
        summary = summarise_general(general)
        text = format_post("general", summary)
        post_to_telegram(text, general["link"])
        seen.add(general["link"])
        posted += 1

    if posted == 0:
        print("Nothing new to post.")
    else:
        print(f"Posted {posted} item(s) to Telegram.")
    save_seen(seen)

if __name__ == "__main__":
    main()
