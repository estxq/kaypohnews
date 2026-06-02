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

# FINANCE sources - markets, investing, banking, insurance, personal finance.
# Business Times is Singapore's financial newspaper, so it's a clean fit.
FINANCE_FEEDS = [
    "https://www.businesstimes.com.sg/rss/top-stories",   # Business Times
]

# GENERAL sources - everyday Singapore + world news.
GENERAL_FEEDS = [
    "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml",  # CNA top
    "https://www.straitstimes.com/news/singapore/rss.xml",                    # ST Singapore
]

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

def fetch_top_new(feeds, seen):
    """Return the first article (from the given feeds) we haven't posted yet,
    or None if there's nothing new."""
    for url in feeds:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            link = entry.get("link")
            if not link or link in seen:
                continue
            return {
                "title": entry.get("title", "").strip(),
                "summary": entry.get("summary", "").strip()[:400],
                "link": link,
            }
    return None

# ----------------------------------------------------------------------
# 4. ASK CLAUDE TO SUMMARISE + CLASSIFY
# ----------------------------------------------------------------------

def summarise_one(article, category):
    """Write a one-sentence summary for a single article.
    category is 'finance' or 'general' and shapes the angle."""
    client = OpenAI(api_key=OPENAI_API_KEY)

    if category == "finance":
        lens = ("Summarise from a finance angle - markets/investing, banking and "
                "interest rates, insurance, or personal finance (savings, loans, CPF). "
                "Focus on the money/investment implication.")
    else:
        lens = "Summarise the key point in plain English for a general reader."

    prompt = f"""You are the editor of "Kaypoh News", a Singapore news digest.

Article title: {article['title']}
Article blurb: {article['summary']}

Write ONE clear sentence summarising this story. {lens}
Respond with ONLY valid JSON: {{"summary": "..."}}"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    data = json.loads(response.choices[0].message.content)
    return data["summary"]

# ----------------------------------------------------------------------
# 5. POST TO TELEGRAM
# ----------------------------------------------------------------------

def format_post(category, summary):
    label = "💰 <b>Finance</b>" if category == "finance" else "📰 <b>General</b>"
    return f'{label}\n{html.escape(summary)}'

def post_to_telegram(text, url):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Tell Telegram exactly which URL to preview, and prefer the BIG image.
    link_preview = json.dumps({
        "url": url,
        "prefer_large_media": True,
        "show_above_text": False,   # summary on top, photo card below
    })

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

    # Grab the top unseen story from each side.
    picks = []
    finance = fetch_top_new(FINANCE_FEEDS, seen)
    if finance:
        picks.append(("finance", finance))
    general = fetch_top_new(GENERAL_FEEDS, seen)
    if general:
        picks.append(("general", general))

    if not picks:
        print("No new articles. Nothing to post.")
        return

    for category, article in picks:
        print(f"Summarising {category}: {article['title']}")
        summary = summarise_one(article, category)
        text = format_post(category, summary)
        post_to_telegram(text, article["link"])
        seen.add(article["link"])
        time.sleep(1)  # gentle pause so Telegram doesn't rate-limit

    print(f"Posted {len(picks)} items to Telegram.")
    save_seen(seen)

if __name__ == "__main__":
    main()
