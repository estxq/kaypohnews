#!/usr/bin/env python3
"""
Kaypoh News bot - v1
--------------------
Pulls Singapore news from RSS feeds, asks Claude to summarise and sort
them into Finance / General, then posts a tidy digest to a Telegram channel.

Secrets are read from environment variables (set these as GitHub Secrets
when you deploy, never hard-code them):
    OPENAI_API_KEY       - your OpenAI API key
    TELEGRAM_BOT_TOKEN   - your bot token from BotFather
    TELEGRAM_CHANNEL     - your channel, e.g. @kaypohnews
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

# Your news sources. Add/remove RSS feed URLs freely.
FEEDS = [
    "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml",  # CNA top
    "https://www.businesstimes.com.sg/rss/top-stories",                       # Business Times
    "https://www.straitstimes.com/news/singapore/rss.xml",                    # ST Singapore
]

# How many new items to include in one digest (keeps posts readable).
MAX_ITEMS_PER_RUN = 12

# OpenAI model - GPT-4.1 Mini is cheap and good at summarising.
MODEL = "gpt-4.1-mini"

# File that remembers what we've already posted (so we don't repeat).
SEEN_FILE = "seen.json"

# Secrets from environment
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
    # Keep the file from growing forever - last 1000 URLs is plenty.
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen)[-1000:], f)

# ----------------------------------------------------------------------
# 3. PULL NEW ARTICLES FROM RSS
# ----------------------------------------------------------------------

def fetch_new_articles(seen):
    new = []
    for url in FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            link = entry.get("link")
            if not link or link in seen:
                continue
            new.append({
                "title": entry.get("title", "").strip(),
                "summary": entry.get("summary", "").strip()[:400],
                "link": link,
            })
    # Newest sources first, capped so digests stay readable.
    return new[:MAX_ITEMS_PER_RUN]

# ----------------------------------------------------------------------
# 4. ASK CLAUDE TO SUMMARISE + CLASSIFY
# ----------------------------------------------------------------------

def summarise_with_ai(articles):
    client = OpenAI(api_key=OPENAI_API_KEY)

    # We hand the model the raw list and ask for clean JSON back.
    article_blob = json.dumps(articles, ensure_ascii=False, indent=2)

    prompt = f"""You are the editor of "Kaypoh News", a Singapore news digest.

Here are new articles as JSON (title, summary, link):

{article_blob}

For each article, write a ONE-sentence plain-English summary and decide if it
is "finance" or "general". Where you spot a plausible knock-on effect between
stories (e.g. fuel prices rising -> ride-hailing fares up), mention it briefly
in the summary, but only if the articles genuinely support it.

Respond with ONLY valid JSON in this exact shape:
{{
  "finance": [{{"summary": "...", "link": "..."}}],
  "general": [{{"summary": "...", "link": "..."}}]
}}"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},  # forces valid JSON back
    )

    text = response.choices[0].message.content
    return json.loads(text)

# ----------------------------------------------------------------------
# 5. POST TO TELEGRAM
# ----------------------------------------------------------------------

def build_messages(digest):
    """Turn the digest into a LIST of messages - one per article."""
    messages = []

    for item in digest.get("finance", []):
        s = html.escape(item["summary"])
        messages.append(f'💰 <b>Finance</b>\n<a href="{item["link"]}">{s}</a>')

    for item in digest.get("general", []):
        s = html.escape(item["summary"])
        messages.append(f'📰 <b>General</b>\n<a href="{item["link"]}">{s}</a>')

    return messages

def post_to_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHANNEL,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()

    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req) as resp:
        result = json.load(resp)
        if not result.get("ok"):
            raise RuntimeError(f"Telegram error: {result}")

# ----------------------------------------------------------------------
# 6. MAIN
# ----------------------------------------------------------------------

def main():
    seen = load_seen()
    articles = fetch_new_articles(seen)

    if not articles:
        print("No new articles. Nothing to post.")
        return

    print(f"Found {len(articles)} new articles. Summarising...")
    digest = summarise_with_ai(articles)

    messages = build_messages(digest)
    for msg in messages:
        post_to_telegram(msg)
        time.sleep(1)  # gentle pause so Telegram doesn't rate-limit
    print(f"Posted {len(messages)} items to Telegram.")

    # Mark everything we just handled as seen.
    for a in articles:
        seen.add(a["link"])
    save_seen(seen)

if __name__ == "__main__":
    main()
