#!/usr/bin/env python3
"""
Kaypoh LinkedIn Assistant  (the always-on half)
------------------------------------------------
Runs continuously (e.g. on a free Google Cloud e2-micro VM) using the SAME bot
token as news_bot.py. news_bot.py only *sends* the news; this script only
*listens* and replies, so they never conflict.

Flows
  1) NEWS  -> she forwards a news post (from the channel, or anywhere) to this
     bot in their private chat. This script reads the forwarded text, asks
     Make (OpenAI) for 3 opinion angles, and shows them as options. She picks
     one, edits it by typing, then taps the "✅ Post to LinkedIn" button.
  2) PHOTO -> she sends a photo to the bot in a private chat, gives keywords,
     gets caption options, picks one, edits, taps "✅ Post to LinkedIn".
  3) QUEUE + SCHEDULE -> she feeds in lots of finished posts ahead of time
     (/add), tags each with a category (Finance, Lifestyle...), and picks her
     weekly posting times (/schedule, any day, 10 AM - 10 PM). At each time the
     bot posts the next queued item by itself, alternating categories. (store.py
     holds the rules and the SQLite file that remembers it all across restarts.)
  4) CHAT -> she can simply talk to the bot ("schedule this for Friday 7pm",
     "make it shorter", "what's in my queue?"). brain.py interprets it. Needs
     OPENAI_API_KEY; without it everything above still works via buttons.

Make.com does the AI suggestions and the LinkedIn posting (two webhooks).
Text-only posts (no link, no photo) are sent to Make as mode="plain", which
needs its own route in the publish scenario (Create a User Text Post).

Setup
  pip install -r requirements.txt
  export OPENAI_API_KEY="optional - switches on the chat brain (brain.py)"
  export BOT_MODEL="optional - chat model, default gpt-4.1-mini"
  export BOT_TZ="optional - her timezone for schedules, default Asia/Singapore"
  export KAYPOH_DB="optional - where the queue/schedule file lives, default ./data/kaypoh.db"
  export TELEGRAM_BOT_TOKEN="same token as news_bot.py"
  export MAKE_SUGGEST_URL="Make webhook that returns {'options':[...]} - SHARED by everyone"
  export MAKE_PUBLISH_URL="default/fallback publish webhook (Lilin's)"
  export OWNER_TELEGRAM_ID="numeric Telegram id(s) with standing access even without a
                            Data Store row, comma-separated. Normally just Lilin + you
                            (the admin) - e.g. 111111111,222222222"
  export MAKE_LOOKUP_URL="Make scenario that looks up {telegram_id} -> {'url': ...} in
                          the publish_routes Data Store (see onboarding tutorial)"
  export PUBLISH_ROUTES="optional legacy fallback, only used if MAKE_LOOKUP_URL is unset
                         or errors. Format: telegram_id:webhook_url, comma-separated."
  python assistant.py

Both flows start with her messaging the bot directly (forwarding a post, or
sending a photo), so Telegram always allows the reply - no need to press
Start first, though /start still gives a quick how-to.

MULTI-PERSON SUPPORT / TRULY SELF-SERVE ONBOARDING: the Suggest webhook
(MAKE_SUGGEST_URL) is generic and shared by everyone - it never touches
anyone's LinkedIn. Publishing is per-person, decided by the Make Data Store
(publish_routes, looked up via MAKE_LOOKUP_URL): being registered there is
BOTH the access check AND the routing decision. Onboarding a new person is
therefore a pure Make.com task - connect their LinkedIn, clone their publish
scenario, add one row to publish_routes - nobody ever needs to touch this VM,
not even once. OWNER_TELEGRAM_ID is now only a small standing exception list
(Lilin, who has no Data Store row and uses MAKE_PUBLISH_URL by default; and
the admin). See the onboarding tutorial for the exact Make setup.

SECURITY: every flow requires EITHER a Data Store row (via MAKE_LOOKUP_URL)
OR membership in OWNER_TELEGRAM_ID.
"""

import os
import re
import time
import html
import logging
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import telebot
from telebot import types

import store as store_mod
from brain import Brain

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kaypoh")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]   # SAME token as news_bot.py
MAKE_SUGGEST_URL   = os.environ["MAKE_SUGGEST_URL"]     # returns AI options - shared by everyone
MAKE_PUBLISH_URL   = os.environ["MAKE_PUBLISH_URL"]     # default/fallback publish webhook (Lilin's)
# Optional: a Make scenario that looks up {telegram_id} -> {"url": ...} in the
# publish_routes Data Store. Being found there grants BOTH access and routing,
# so new people never need a VM edit. See MULTI-PERSON SUPPORT above.
MAKE_LOOKUP_URL    = os.environ.get("MAKE_LOOKUP_URL")
# Small standing exception list (Lilin + admin) who don't need a Data Store row.
OWNER_TELEGRAM_IDS = {int(x) for x in os.environ["OWNER_TELEGRAM_ID"].split(",") if x.strip()}

# Legacy fallback only: "id:url,id:url,...", used if MAKE_LOOKUP_URL is unset
# or errors/misses. New people should go in the Data Store, not here.
PUBLISH_ROUTES = {}
for _pair in os.environ.get("PUBLISH_ROUTES", "").split(","):
    _pair = _pair.strip()
    if not _pair:
        continue
    _uid_str, _url = _pair.split(":", 1)
    PUBLISH_ROUTES[int(_uid_str.strip())] = _url.strip()

# Cache Data Store lookups briefly so every single bot interaction (several
# handlers may check per message) doesn't each hit Make over the network, while
# still picking up newly-added people within a few minutes, no restart needed.
_lookup_cache = {}   # uid -> (url_or_None, expires_at)
_LOOKUP_HIT_TTL = 300    # seconds a found row is remembered
_LOOKUP_MISS_TTL = 20    # a genuine "not registered" is re-checked quickly, so a person
                         # you just added doesn't wait minutes to be let in

def _data_store_lookup(uid):
    """Returns this person's publish webhook URL if they have a row in the
    Make Data Store, else None. This single check is both the security gate
    and the routing decision for anyone who isn't in OWNER_TELEGRAM_ID.
    A failed lookup (Make unreachable) is NOT remembered, so one blip can't lock
    someone out for minutes."""
    if not MAKE_LOOKUP_URL:
        return None
    now = time.time()
    cached = _lookup_cache.get(uid)
    if cached and now < cached[1]:
        return cached[0]
    try:
        r = requests.post(MAKE_LOOKUP_URL, json={"telegram_id": uid}, timeout=15)
        r.raise_for_status()
        url = (r.json() or {}).get("url") or None
    except Exception:
        log.warning("Data Store lookup failed for %s", uid)
        return None
    _lookup_cache[uid] = (url, now + (_LOOKUP_HIT_TTL if url else _LOOKUP_MISS_TTL))
    return url

def publish_url_for(uid):
    """Where should THIS user's posts go? Data Store row first (self-serve),
    then the legacy static fallback, then Lilin's default."""
    return _data_store_lookup(uid) or PUBLISH_ROUTES.get(uid, MAKE_PUBLISH_URL)

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

TZ_NAME = os.environ.get("BOT_TZ", "Asia/Singapore")
TZ = ZoneInfo(TZ_NAME)
TZ_LABEL = "Singapore time" if TZ_NAME == "Asia/Singapore" else TZ_NAME

DB_PATH = os.environ.get("KAYPOH_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "kaypoh.db")
db = store_mod.Store(DB_PATH)       # queue + weekly schedule, survives restarts
brain = Brain()                     # free-text understanding (off without OPENAI_API_KEY)

def is_owner(uid):
    """Allowed if in the small standing exception list, OR registered in the
    Data Store - so a brand-new person never needs a VM edit to get access."""
    return uid in OWNER_TELEGRAM_IDS or _data_store_lookup(uid) is not None

def guard(message):
    """True if this person may use the bot; otherwise tells them and returns False."""
    if is_owner(message.from_user.id):
        return True
    bot.reply_to(message, "This bot is private.")
    return False

# State keyed by user id (same as their private chat id). This is only the
# in-progress conversation (drafts, which buttons are open...). Anything that must
# survive a restart - the queue, the schedule - lives in `db` instead.
STATE = {}

def get_state(uid):
    return STATE.setdefault(uid, {"stage": None, "draft": None, "options": None,
                                  "image_url": None, "photo_file_id": None,
                                  "mode": None, "article_url": None,
                                  "article_title": None, "article_desc": None,
                                  "article_image": None, "source": None,
                                  "history": []})

def reset_flow(uid):
    """Clear the draft/flow state but keep the chat memory."""
    old = STATE.pop(uid, None)
    if old and old.get("history"):
        get_state(uid)["history"] = old["history"]


def extract_url(message):
    """Pull the article link out of a forwarded post so it can be cited on
    LinkedIn. news_bot.py adds the link as a text_link; articles forwarded from
    elsewhere usually carry a plain url entity or a link-preview url instead."""
    text = message.text or message.caption or ""
    for e in (message.entities or []) + (message.caption_entities or []):
        if e.type == "text_link" and getattr(e, "url", None):
            return e.url
        if e.type == "url":
            return text[e.offset:e.offset + e.length]
    lpo = getattr(message, "link_preview_options", None)
    if lpo is not None and getattr(lpo, "url", None):
        return lpo.url
    return None


def fetch_article_meta(url, limit=2000):
    """Download an article and pull out its main text plus title/description.
    Text lets the AI react to a bare link; title/description feed the LinkedIn
    preview card. Returns None on any failure so callers can fall back."""
    try:
        import trafilatura
    except ImportError:
        return None
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        meta = trafilatura.extract_metadata(downloaded)
        return {
            "text": (text.strip()[:limit] if text else None),
            "title": (getattr(meta, "title", None) if meta else None),
            "description": (getattr(meta, "description", None) if meta else None),
            "image": (getattr(meta, "image", None) if meta else None),
        }
    except Exception:
        return None


def resolve_opinion_source(uid, message):
    """Decide what to comment on and gather link-card details.
    Returns (content, url, title, description, image). If she basically just
    sent a link, the article text is fetched for the AI; either way, when a link
    is present we grab its title/description/image for the LinkedIn preview card."""
    url = extract_url(message)
    text = (message.text or message.caption or "").strip()
    remainder = text.replace(url, "").strip() if url else text
    bare_link = bool(url) and len(remainder) < 80
    title = desc = image = None
    if url:
        if bare_link:
            bot.send_message(uid, "🔗 Reading the article…")
        meta = fetch_article_meta(url)
        if meta:
            title, desc, image = meta.get("title"), meta.get("description"), meta.get("image")
            body = meta.get("text") or ""
            if bare_link:
                # Require real article length so a blocked/short scrape (e.g. a
                # cookie notice) never gets passed to the AI as "the article" -
                # that's what made it echo the prompt's example text verbatim.
                if len(body) >= 200:
                    return body, url, title, desc, image
                # Fall back to title + description, which are still real content.
                fallback = " — ".join(p for p in (title, desc) if p)
                if len(fallback) >= 40:
                    return fallback, url, title, desc, image
                bot.send_message(uid, "(That site blocked me from reading the full article — "
                                      "paste a short summary and I'll use that instead.)")
                return None, url, title, desc, image
        elif bare_link:
            bot.send_message(uid, "(Couldn't read that link — paste a short summary instead.)")
            return None, url, title, desc, image
    return text, url, title, desc, image


# ---------------------------------------------------------------- Make helpers
def ask_make_for_suggestions(mode, content):
    """mode = 'opinion' or 'caption'. Returns a list of option strings."""
    r = requests.post(MAKE_SUGGEST_URL, json={"mode": mode, "content": content}, timeout=60)
    r.raise_for_status()
    return r.json().get("options", [])

def publish_to_linkedin(publish_url, mode, text, image_url=None, link=None, link_title=None,
                        link_desc=None, link_image=None):
    """mode = 'text' (opinion; when link/title given, Make posts it as an Article
    share with a preview card) or 'image' (photo). publish_url is THIS user's own
    publish webhook (see publish_url_for), so each person's posts go to their own
    LinkedIn. Returns Make's JSON response."""
    r = requests.post(publish_url,
                      json={"mode": mode, "text": text, "image_url": image_url,
                            "link": link, "link_title": link_title, "link_desc": link_desc,
                            "link_image": link_image},
                      timeout=60)
    r.raise_for_status()
    return r.json()

def show_options(uid, header, options, label):
    """Send the header, then each option as its OWN message with a bold
    "<label> N" title (e.g. POV 1) and its own pick button. Separate bubbles
    keep long, LinkedIn-length drafts easy to tell apart and to copy."""
    n = len(options)
    try:
        bot.send_message(
            uid,
            f"<b>{html.escape(header, quote=False)}</b>\n"
            f"({n} to choose from — scroll down 👇)",
            parse_mode="HTML",
        )
    except Exception:
        bot.send_message(uid, f"{header}\n({n} to choose from — scroll down 👇)")
    # Each option is its OWN bubble with its own pick button. Sends are made
    # resilient so a single long or oddly-formatted option can't stop the rest:
    # try HTML first, then fall back to plain text, chunking to Telegram's limit.
    for i, o in enumerate(options):
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(f"✍️ Use {label} {i + 1}", callback_data=f"pick:{i}"))
        title = f"{label} {i + 1} of {n}"
        html_text = f"<b>{title}</b>\n\n{html.escape(o, quote=False)}"
        try:
            if len(html_text) > 4000:
                raise ValueError("too long for one message")
            bot.send_message(uid, html_text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            plain = f"{title}\n\n{o}"
            if len(plain) > 4000:
                plain = plain[:3990] + "…"
            try:
                bot.send_message(uid, plain, reply_markup=kb)
            except Exception:
                bot.send_message(uid, plain[:3990], reply_markup=kb)
    # A regenerate button so she can get a whole fresh set without re-forwarding
    # the article or re-uploading the photo (the source is kept in state).
    regen_kb = types.InlineKeyboardMarkup()
    regen_kb.add(types.InlineKeyboardButton("🔄 Regenerate (fresh set)", callback_data="regen"))
    bot.send_message(uid, "Not quite right?", reply_markup=regen_kb)

def draft_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Post now", callback_data="do_post"))
    kb.add(types.InlineKeyboardButton("📌 Pick a day & time", callback_data="pdt"))
    kb.add(types.InlineKeyboardButton("🗓 Add to my queue (auto-posts in my schedule)", callback_data="qadd"))
    return kb

def send_draft(uid):
    """Show the draft as ONE clean message — just the post text, with the ✅
    button attached below it. No instruction words, so a long-press -> Copy
    grabs exactly the post and nothing else to delete. (For opinion posts the
    article link is still added automatically when she posts.)"""
    st = get_state(uid)
    bot.send_message(uid, st["draft"], reply_markup=draft_keyboard())

def publish_core(uid, text, photo_url=None, link=None, link_title=None, link_desc=None,
                 link_image=None):
    """Send ONE post to THIS person's LinkedIn through Make and return its URL ('' if
    Make didn't send one). Raises on failure. Used by the ✅ button, the queue and the
    scheduler, so they can never behave differently.

    Which route in the Make publish scenario handles it:
      photo            -> mode "image"  (Create a User Image Post)
      link (article)   -> mode "text"   (Article share, so LinkedIn shows a preview card)
      neither          -> mode "plain"  (Create a User Text Post - plain text)
    """
    if photo_url:
        mode = "image"
    elif link:
        mode = "text"
        link_title = (link_title or "Read the article")[:400]
    else:
        mode = "plain"
    try:
        result = publish_to_linkedin(publish_url_for(uid), mode, text, photo_url,
                                     link, link_title, link_desc, link_image)
    except Exception as e:
        if mode == "plain":
            raise RuntimeError(
                f"{e}\n\nText-only posts need a 'plain' route in your Make publish scenario "
                f"(a Create a User Text Post module with the filter mode = plain). "
                f"Posts with a link or a photo are not affected.") from e
        raise
    return (result or {}).get("url", "")

def do_publish(uid, notify):
    """notify(text) sends feedback back to her. Shared by the button and /post."""
    st = get_state(uid)
    if not st.get("draft"):
        notify("No draft yet. Forward an article, pick a perspective, or send a photo first.")
        return
    notify("Posting to LinkedIn…")
    try:
        url = publish_core(uid, st["draft"], st.get("image_url"), st.get("article_url"),
                           st.get("article_title"), st.get("article_desc"), st.get("article_image"))
    except Exception as e:
        bot.send_message(uid, f"Posting failed: {e}")
        return
    bot.send_message(uid, "✅ Posted!" + (f"\n{url}" if url else ""))
    reset_flow(uid)


# ---------------------------------------------------------------- /start
HELP_TEXT = (
    "Here's what I can do:\n\n"
    "✍️ Create a post\n"
    "• Forward me a news article (or paste a link) and I'll suggest 3 angles.\n"
    "• Or send a photo and tell me what happened, and I'll write the caption.\n"
    "• Pick one, edit it by sending your version, then tap ✅ Post to LinkedIn.\n\n"
    "🗓 Post now, or later - your choice, every time\n"
    "• Under every draft: ✅ Post now, 📌 Pick a day & time (just this one post), or "
    "“Add to my queue” to let your weekly schedule post it.\n"
    "• Or use /add to feed me lots of finished "
    "posts at once and tag each with a category (Finance, Lifestyle…).\n"
    "• /schedule — pick your weekly posting days and times (10 AM–10 PM). At each "
    "time I post the next one and alternate the categories.\n"
    "• /queue — see what's waiting and what goes out next. Tap “See all / edit” to read, "
    "reword, re-categorise or delete any queued post.\n\n"
    "💬 You can also just talk to me: “schedule this for Friday 7pm”, “make it "
    "shorter”, “what's in my queue?”, “post about CPF changes”."
)


@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    uid = message.from_user.id
    get_state(uid)
    bot.reply_to(message, "Hi! I help you write LinkedIn posts and post them for you.\n\n" + HELP_TEXT
                 if message.text.startswith("/start") else HELP_TEXT)


# ---------------------------------------------------------------- NEWS: opinion helper
def _looks_like_placeholder(text):
    """Catch the model echoing the prompt's own example ("post 1", "caption 2",
    "take 3"...) instead of writing real content - happens when it's given too
    little to react to."""
    t = (text or "").strip().lower().rstrip(".!")
    return len(t) < 25 or t in {f"{w} {i}" for w in ("post", "caption", "take", "option")
                                for i in ("1", "2", "3")}

def generate_opinions(uid, content, article_url=None, article_title=None,
                      article_desc=None, article_image=None):
    """Turn an article (forwarded, typed or pasted) into 3 opinion angles.
    Reused for the first go and for every regenerate."""
    if not content:
        bot.send_message(uid, "I don't have enough to work with — send the article text "
                              "or forward the original post.")
        return
    bot.send_chat_action(uid, "typing")
    # With long article text the model sometimes returns fewer than 3 real
    # angles (or echoes the prompt's own example); retry a couple of times so
    # she reliably gets a full, real set without having to hit Regenerate.
    options = []
    for _ in range(3):
        try:
            got = ask_make_for_suggestions("opinion", content)
        except Exception as e:
            if not options:
                bot.send_message(uid, f"Sorry, couldn't get suggestions: {e}")
                return
            break
        got = [o for o in got if not _looks_like_placeholder(o)]
        if len(got) > len(options):
            options = got
        if len(options) >= 3:
            break
    if not options:
        bot.send_message(uid, "Couldn't generate real suggestions from that — try pasting "
                              "a short summary of the article instead of just the link.")
        return
    get_state(uid).update(mode="opinion", options=options, image_url=None, draft=None,
                          stage="choosing", article_url=article_url,
                          article_title=article_title, article_desc=article_desc,
                          article_image=article_image, source=content)
    show_options(uid, "Here are some angles you could post:", options, "POV")


# ---------------------------------------------------------------- NEWS: article forwarded
@bot.message_handler(func=lambda m: m.forward_date is not None, content_types=["text"])
def on_forwarded_article(message):
    uid = message.from_user.id
    if not is_owner(uid):
        bot.reply_to(message, "This bot is private.")
        return
    if get_state(uid).get("stage") == "bulk_add":
        bulk_queue_text(uid, message)       # feeding the queue: a forward is just content
        return
    content, url, title, desc, image = resolve_opinion_source(uid, message)
    if not content:
        bot.reply_to(message, "Couldn't read any text or link from that — try forwarding the original post.")
        return
    generate_opinions(uid, content, url, title, desc, image)


# ---------------------------------------------------------------- PHOTO flow
@bot.message_handler(content_types=["photo"])
def on_photo(message):
    uid = message.from_user.id
    if not is_owner(uid):
        bot.reply_to(message, "This bot is private.")
        return
    file_id = message.photo[-1].file_id           # highest resolution
    if get_state(uid).get("stage") == "bulk_add":
        bulk_queue_photo(uid, message, file_id)   # feeding the queue: photo + her own caption
        return
    image_url = photo_url_for(file_id)
    st = get_state(uid)
    # photo_file_id is kept too: the download link above expires, the file id doesn't,
    # so a queued photo can still be posted days later.
    st.update(mode="caption", image_url=image_url, photo_file_id=file_id,
              stage="awaiting_keywords", options=None, draft=None, article_url=None)
    bot.reply_to(
        message,
        "Nice photo! 📸\n\n"
        "Tell me what happened — the more you dump, the better the post:\n"
        "• What was the event or moment?\n"
        "• Who was there (names)?\n"
        "• What did you do / achieve?\n"
        "• Any funny, memorable or honest bits, and how you felt?\n\n"
        "Just type it all out casually and I'll shape it into a LinkedIn story.",
    )


# ---------------------------------------------------------------- option picker
def select_option(uid, idx):
    """Load option number idx into the draft. False if it's no longer available."""
    st = get_state(uid)
    options = st.get("options") or []
    if not 0 <= idx < len(options):
        return False
    st["draft"] = options[idx]
    st["stage"] = "editing"
    send_draft(uid)
    return True


@bot.callback_query_handler(func=lambda c: c.data.startswith("pick:"))
def on_pick(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    if not select_option(uid, int(call.data.split(":", 1)[1])):
        bot.answer_callback_query(call.id, "That option expired, try again.")
        return
    bot.answer_callback_query(call.id, "Loaded into your draft.")


# ---------------------------------------------------------------- 🔄 Regenerate
@bot.callback_query_handler(func=lambda c: c.data == "regen")
def on_regen(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    st = get_state(uid)
    src, mode = st.get("source"), st.get("mode")
    if not src or mode not in ("opinion", "caption"):
        bot.answer_callback_query(call.id, "Nothing to regenerate yet.")
        return
    bot.answer_callback_query(call.id, "Fresh set coming up…")
    bot.send_chat_action(uid, "typing")
    try:
        options = ask_make_for_suggestions(mode, src)
    except Exception as e:
        bot.send_message(uid, f"Sorry, couldn't regenerate: {e}")
        return
    st.update(options=options, stage="choosing")
    if mode == "opinion":
        show_options(uid, "Here are some fresh angles:", options, "POV")
    else:
        show_options(uid, "Here are some fresh caption ideas:", options, "Caption")


# ---------------------------------------------------------------- ✅ Post to LinkedIn button
@bot.callback_query_handler(func=lambda c: c.data == "do_post")
def on_post_button(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    bot.answer_callback_query(call.id, "Posting…")
    do_publish(uid, lambda t: bot.send_message(uid, t))


# ---------------------------------------------------------------- /post (fallback)
@bot.message_handler(commands=["post"])
def cmd_post(message):
    uid = message.from_user.id
    if not is_owner(uid):
        bot.reply_to(message, "This bot is private.")
        return
    do_publish(uid, lambda t: bot.reply_to(message, t))


# ---------------------------------------------------------------- caption helper
def generate_captions(uid, keywords):
    """Turn her notes into caption options. Reused for the first go AND for
    every regenerate, so she can keep resending (tweaked) notes for a fresh set
    without having to upload the photo again."""
    bot.send_chat_action(uid, "typing")
    try:
        options = ask_make_for_suggestions("caption", keywords)
    except Exception as e:
        bot.send_message(uid, f"Sorry, couldn't get captions: {e}")
        return
    get_state(uid).update(options=options, stage="choosing", source=keywords)
    show_options(uid, "Here are some caption ideas:", options, "Caption")


# ================================================================ QUEUE + SCHEDULE
# Finished posts wait in `db` (category-tagged). Her weekly slots decide WHEN; at
# each slot the scheduler posts the next item, alternating categories. Everything
# is keyed by her Telegram id, so every person's queue and schedule stay separate.

def one_line(text, n=80):
    s = " ".join((text or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def local_from_utc_str(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).astimezone(TZ)


def fmt_dt(dt):
    return (f"{store_mod.DAY_LABELS[dt.weekday()]} {dt.day} {dt.strftime('%b')}, "
            f"{store_mod.fmt_time(dt.strftime('%H:%M'))}")


def photo_url_for(file_id):
    """A fresh download link for a Telegram photo (links expire, file ids don't)."""
    info = bot.get_file(file_id)
    return f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{info.file_path}"


def describe_next(uid):
    if db.is_paused(uid):
        return "Scheduling is paused right now (/schedule to resume)."
    nxt = db.next_slot(uid, datetime.now(TZ))
    if nxt is None:
        return "You haven't picked posting times yet - /schedule to choose them."
    return f"Next scheduled post: {fmt_dt(nxt)}."


def counts_line(uid):
    counts = db.counts(uid)
    return " · ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "empty"


def btn(label, data):
    return types.InlineKeyboardButton(label, callback_data=data)


# ---------------------------------------------------------------- queueing a draft
def queue_current_draft(uid, category, scheduled_for=None):
    """Move the draft she's working on into the queue (or pin it to one date/time
    when scheduled_for - a naive UTC datetime - is given)."""
    st = get_state(uid)
    text = st.get("draft")
    if not text:
        bot.send_message(uid, "I don't have a draft open to queue. Make one first - forward "
                              "an article or send a photo - or use /add to feed me finished posts.")
        return False
    if st.get("image_url") and not st.get("photo_file_id"):
        bot.send_message(uid, "I lost track of that photo - please send it again.")
        return False
    item_id = db.add_item(
        uid, category, text,
        photo_file_id=st.get("photo_file_id") if st.get("image_url") else None,
        link=st.get("article_url"), link_title=st.get("article_title"),
        link_desc=st.get("article_desc"), link_image=st.get("article_image"),
        scheduled_for=scheduled_for)
    item = db.get_item(item_id)
    reset_flow(uid)
    if scheduled_for is not None:
        when = fmt_dt(scheduled_for.replace(tzinfo=timezone.utc).astimezone(TZ))
        bot.send_message(uid, f"📌 Pinned for {when} ({TZ_LABEL}) under {item['category']}. "
                              f"It will post by itself at that time.")
    else:
        counts = db.counts(uid)
        bot.send_message(uid, f"🗓 Added to your {item['category']} queue - "
                              f"{counts.get(item['category'], 0)} waiting there, "
                              f"{sum(counts.values())} in total.\n{describe_next(uid)}")
    return True


# ---------------------------------------------------------------- posting a queue item
def post_queue_item(item):
    """Publish one queue item now. Returns (ok, post_url_or_error_text)."""
    uid = item["uid"]
    if uid not in OWNER_TELEGRAM_IDS and not _data_store_lookup(uid):
        # without this, a removed person's leftovers would fall back to Lilin's account
        db.mark_failed(item["id"], "Not registered with the bot")
        return False, "your account isn't registered with the bot (ask the admin to add you again)"
    if not db.claim(item["id"]):
        return False, "that post was already handled"
    try:
        photo_url = photo_url_for(item["photo_file_id"]) if item["photo_file_id"] else None
        url = publish_core(uid, item["text"], photo_url, item["link"], item["link_title"],
                           item["link_desc"], item["link_image"])
    except Exception as e:
        log.exception("posting queue item %s failed", item["id"])
        db.mark_failed(item["id"], e)
        return False, str(e)
    db.mark_posted(item["id"], url)
    return True, url


def announce_result(item, ok, detail, when_label):
    uid = item["uid"]
    preview = one_line(item["text"], 90)
    try:
        if ok:
            left = sum(db.counts(uid).values())
            msg = f"✅ Posted your {item['category']} post ({when_label}):\n“{preview}”"
            if detail:
                msg += f"\n{detail}"
            if left == 0:
                msg += "\n\nYour queue is now empty - send me more with /add so the schedule keeps going."
            elif left <= 2:
                msg += f"\n\nHeads up: only {left} left in your queue - /add more soon."
            bot.send_message(uid, msg)
        else:
            kb = types.InlineKeyboardMarkup()
            kb.add(btn("🔁 Retry now", f"qretry:{item['id']}"))
            bot.send_message(uid, f"⚠️ Couldn't post your {item['category']} post ({when_label}):\n"
                                  f"“{preview}”\n\n{detail}", reply_markup=kb)
    except Exception:
        log.exception("could not notify %s", uid)


# ---------------------------------------------------------------- categories
def category_choices(uid):
    seen, out = set(), []
    for name in db.categories(uid) + store_mod.DEFAULT_CATEGORIES:
        key = store_mod.category_key(name)
        if key not in seen:
            seen.add(key)
            out.append(name)
    return out


def ask_category(uid, prompt, prefix):
    names = category_choices(uid)
    get_state(uid)["cat_choices"] = names
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[btn(n, f"{prefix}:{i}") for i, n in enumerate(names)])
    kb.row(btn("➕ New category", f"{prefix}:new"))
    bot.send_message(uid, prompt, reply_markup=kb)


def ask_new_category(uid, pending):
    st = get_state(uid)
    if st.get("stage") != "awaiting_category_name":
        st["return_stage"] = st.get("stage")
    st["stage"] = "awaiting_category_name"
    st["pending"] = pending
    bot.send_message(uid, "What should the new category be called? (for example Travel or "
                          "Wellness) - or type cancel.")


def handle_category_name(uid, text):
    st = get_state(uid)
    if text.strip().lower() == "cancel":
        st["stage"] = st.pop("return_stage", None)
        st.pop("pending", None)
        bot.send_message(uid, "No problem, cancelled.")
        return
    if len(text.strip()) > 30 or "\n" in text:
        bot.send_message(uid, "Keep it short - one or two words, like Travel. Try again, or type cancel.")
        return
    name = db.canonical_category(uid, text)
    pending = st.pop("pending", None)
    st["stage"] = st.pop("return_stage", None)
    if pending == "queue_draft":
        queue_current_draft(uid, name)
    elif pending == "bulk":
        start_bulk_mode(uid, name)
    elif pending == "pin":
        finish_pin(uid, name)
    elif pending == "move":
        do_move(uid, name)


def _category_from_callback(call, prefix):
    val = call.data[len(prefix) + 1:]
    if val == "new":
        return "new", None
    try:
        return "ok", (get_state(call.from_user.id).get("cat_choices") or [])[int(val)]
    except (ValueError, IndexError):
        return "gone", None


@bot.callback_query_handler(func=lambda c: c.data == "qadd")
def on_queue_add(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    if not get_state(uid).get("draft"):
        bot.answer_callback_query(call.id, "There's no draft to queue any more.")
        return
    bot.answer_callback_query(call.id)
    ask_category(uid, "Which category is this post?", "qc")


@bot.callback_query_handler(func=lambda c: c.data.startswith("qc:"))
def on_queue_category(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    kind, name = _category_from_callback(call, "qc")
    bot.answer_callback_query(call.id)
    if kind == "new":
        ask_new_category(uid, "queue_draft")
    elif kind == "gone":
        bot.send_message(uid, "That list expired - tap 🗓 Add to queue again.")
    else:
        queue_current_draft(uid, name)


# ---------------------------------------------------------------- bulk add (/add)
def start_bulk_mode(uid, category):
    category = db.canonical_category(uid, category)
    get_state(uid).update(stage="bulk_add", bulk_category=category)
    kb = types.InlineKeyboardMarkup()
    kb.row(btn("✅ Done", "bdone"), btn("🔀 Switch category", "badd"))
    bot.send_message(uid, f"📥 Adding to {category}.\n\nSend me finished posts, one message each - "
                          f"plain text, or a photo with a caption. I'll queue every one under "
                          f"{category}.\n\nTap Done when you're finished.", reply_markup=kb)


def begin_bulk(uid, category=None):
    if category and category.strip():
        start_bulk_mode(uid, category)
    else:
        ask_category(uid, "Which category are you adding posts to?", "bc")


def finish_bulk(uid):
    st = get_state(uid)
    st["stage"] = None
    st.pop("bulk_category", None)
    total = sum(db.counts(uid).values())
    bot.send_message(uid, f"✅ Done. Your queue now has {total} posts ({counts_line(uid)}).\n"
                          f"{describe_next(uid)}")


def bulk_queue_text(uid, message):
    cat = get_state(uid).get("bulk_category") or "General"
    text = (message.text or "").strip()
    url = extract_url(message)
    title = desc = image = None
    if url:
        meta = fetch_article_meta(url) or {}
        title, desc, image = meta.get("title"), meta.get("description"), meta.get("image")
        text = text.replace(url, "").strip()
        if not text:
            bot.send_message(uid, "Add a few words of your own with that link, then send it again.")
            return
    db.add_item(uid, cat, text, link=url, link_title=title, link_desc=desc, link_image=image)
    bot.send_message(uid, f"✅ Queued under {cat} - {db.counts(uid).get(cat, 0)} waiting there.")


def bulk_queue_photo(uid, message, file_id):
    cat = get_state(uid).get("bulk_category") or "General"
    caption = (message.caption or "").strip()
    if not caption:
        bot.reply_to(message, "That photo needs a caption - send it again with the post text "
                              "as the caption.")
        return
    db.add_item(uid, cat, caption, photo_file_id=file_id)
    bot.send_message(uid, f"✅ Queued under {cat} (with photo) - {db.counts(uid).get(cat, 0)} waiting there.")


@bot.message_handler(commands=["add"])
def cmd_add(message):
    if guard(message):
        begin_bulk(message.from_user.id)


@bot.message_handler(commands=["done"])
def cmd_done(message):
    if guard(message) and get_state(message.from_user.id).get("stage") == "bulk_add":
        finish_bulk(message.from_user.id)


@bot.callback_query_handler(func=lambda c: c.data == "badd")
def on_bulk_add_button(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    bot.answer_callback_query(call.id)
    begin_bulk(uid)


@bot.callback_query_handler(func=lambda c: c.data.startswith("bc:"))
def on_bulk_category(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    kind, name = _category_from_callback(call, "bc")
    bot.answer_callback_query(call.id)
    if kind == "new":
        ask_new_category(uid, "bulk")
    elif kind == "gone":
        bot.send_message(uid, "That list expired - send /add again.")
    else:
        start_bulk_mode(uid, name)


@bot.callback_query_handler(func=lambda c: c.data == "bdone")
def on_bulk_done(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    bot.answer_callback_query(call.id)
    finish_bulk(uid)


# ---------------------------------------------------------------- one post, one date + time
# "📌 Pick a day & time": day -> time -> category -> pinned. It posts by itself at
# that moment and stays out of the weekly rotation.
def pin_days_keyboard():
    now = datetime.now(TZ)
    buttons = []
    for offset in range(0, 8):
        if offset == 0 and now.strftime("%H:%M") >= "21:58":
            continue                          # nothing left to pick today
        day = now + timedelta(days=offset)
        label = ("Today" if offset == 0 else "Tomorrow" if offset == 1
                 else f"{store_mod.DAY_LABELS[day.weekday()]} {day.day} {day.strftime('%b')}")
        buttons.append(btn(label, f"pd:{offset}"))
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*buttons)
    return kb


def finish_pin(uid, category):
    st = get_state(uid)
    when = st.get("pin_when")
    if not when or not st.get("draft"):
        bot.send_message(uid, "I lost track of that - tap 📌 Pick a day & time on your draft again.")
        return
    if when <= datetime.now(TZ) + timedelta(minutes=1):
        st.pop("pin_when", None)
        bot.send_message(uid, "That time passed while you were choosing. Pick another day and time:",
                         reply_markup=pin_days_keyboard())
        return
    queue_current_draft(uid, category, scheduled_for=when.astimezone(timezone.utc).replace(tzinfo=None))


@bot.callback_query_handler(func=lambda c: c.data == "pdt")
def on_pin_start(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    if not get_state(uid).get("draft"):
        bot.answer_callback_query(call.id, "There's no draft to schedule any more.")
        return
    bot.answer_callback_query(call.id)
    bot.send_message(uid, f"Which day should it go out? ({TZ_LABEL}, 10 AM - 10 PM)",
                     reply_markup=pin_days_keyboard())


@bot.callback_query_handler(func=lambda c: c.data.startswith("pd:"))
def on_pin_day(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    day = (datetime.now(TZ) + timedelta(days=int(call.data[3:]))).date()
    get_state(uid)["pin_date"] = day.isoformat()
    bot.answer_callback_query(call.id)
    bot.send_message(uid, f"{fmt_dt(datetime(day.year, day.month, day.day, 10, 0, tzinfo=TZ)).rsplit(',', 1)[0]} - "
                          f"what time?\n(Need something like 3:20 PM? Just tell me in chat.)",
                     reply_markup=time_keyboard(prefix="pt"))


@bot.callback_query_handler(func=lambda c: c.data.startswith("pth:"))
def on_pin_time_grid(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=time_keyboard(half=call.data == "pth:30", prefix="pt"))
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("pt:"))
def on_pin_time(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    st = get_state(uid)
    raw = call.data[3:]
    hhmm = f"{raw[:2]}:{raw[2:]}"
    if not st.get("pin_date") or not st.get("draft"):
        bot.answer_callback_query(call.id, "Tap 📌 Pick a day & time on your draft first.")
        return
    day = datetime.strptime(st["pin_date"], "%Y-%m-%d")
    when = datetime(day.year, day.month, day.day, int(hhmm[:2]), int(hhmm[3:]), tzinfo=TZ)
    if when <= datetime.now(TZ) + timedelta(minutes=2):
        bot.answer_callback_query(call.id, "That time has already passed - pick a later one.")
        return
    st["pin_when"] = when
    bot.answer_callback_query(call.id)
    ask_category(uid, f"📌 {fmt_dt(when)} ({TZ_LABEL}). Which category is this post?", "pc")


@bot.callback_query_handler(func=lambda c: c.data.startswith("pc:"))
def on_pin_category(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    kind, name = _category_from_callback(call, "pc")
    bot.answer_callback_query(call.id)
    if kind == "new":
        ask_new_category(uid, "pin")
    elif kind == "gone":
        bot.send_message(uid, "That list expired - tap 📌 Pick a day & time again.")
    else:
        finish_pin(uid, name)


# ---------------------------------------------------------------- /queue
def send_queue_view(uid):
    counts = db.counts(uid)
    pinned = db.list_queued(uid, pinned=True)
    if not counts and not pinned:
        kb = types.InlineKeyboardMarkup()
        kb.row(btn("➕ Add posts", "badd"))
        bot.send_message(uid, "Your queue is empty.\n\nFeed me finished posts with /add (tag each "
                              "with a category), or build a post and tap 🗓 Add to queue.",
                         reply_markup=kb)
        return
    total = sum(counts.values())
    lines = [f"📚 Your queue - {total} waiting" + (f", {len(pinned)} pinned" if pinned else "")]
    if counts:
        lines += ["", "By category: " + counts_line(uid)]
    kb = types.InlineKeyboardMarkup()
    upcoming = db.upcoming(uid, 5)
    if upcoming:
        lines += ["", "Up next, in posting order:"]
        row = []
        for i, it in enumerate(upcoming, 1):
            lines.append(f"{i}. {it['category']} - {one_line(it['text'], 70)}"
                         + ("  📷" if it["photo_file_id"] else ""))
            row.append(btn(f"🗑 {i}", f"qdel:{it['id']}"))
        kb.row(*row)
        if total > len(upcoming):
            lines.append(f"…and {total - len(upcoming)} more")
    if pinned:
        lines += ["", "📌 Pinned to a date:"]
        for it in pinned:
            when = fmt_dt(local_from_utc_str(it["scheduled_for"]))
            lines.append(f"• {when} - {it['category']}: {one_line(it['text'], 50)}")
            kb.row(btn(f"❌ Cancel {when}", f"qdel:{it['id']}"))
    lines += ["", describe_next(uid)]
    if upcoming:
        kb.row(btn("▶ Post next now", "qnow"))
    kb.row(btn("📋 See all / edit", "qlist:0"), btn("➕ Add more", "badd"))
    bot.send_message(uid, "\n".join(lines), reply_markup=kb)


@bot.message_handler(commands=["queue"])
def cmd_queue(message):
    if guard(message):
        send_queue_view(message.from_user.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("qdel:"))
def on_queue_delete(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    ok = db.delete_item(uid, int(call.data[5:]))
    bot.answer_callback_query(call.id, "Removed 🗑" if ok else "Already gone")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    send_queue_view(uid)


@bot.callback_query_handler(func=lambda c: c.data == "qnow")
def on_queue_now(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    item = db.next_item(uid)
    if not item:
        bot.answer_callback_query(call.id, "Your queue is empty.")
        return
    bot.answer_callback_query(call.id, "Posting…")
    ok, detail = post_queue_item(item)
    announce_result(item, ok, detail, "posted now")


@bot.callback_query_handler(func=lambda c: c.data.startswith("qretry:"))
def on_queue_retry(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    item = db.get_item(int(call.data[7:]))
    if not item or item["uid"] != uid or not db.requeue(item["id"]):
        bot.answer_callback_query(call.id, "That one can't be retried any more.")
        return
    bot.answer_callback_query(call.id, "Trying again…")
    ok, detail = post_queue_item(db.get_item(item["id"]))
    announce_result(item, ok, detail, "retry")


# ---------------------------------------------------------------- manage the queue
# See every waiting post (5 per page), open one to read it in full, then edit the
# wording, move it to another category, or delete it.
QUEUE_PAGE = 5


def queue_order(uid):
    """Everything waiting: rotation posts in the order they will go out, then pinned ones."""
    return db.upcoming(uid, 100000) + db.list_queued(uid, pinned=True)


def render_queue_list(uid, page):
    items = queue_order(uid)
    if not items:
        return None, None, 0
    pages = (len(items) + QUEUE_PAGE - 1) // QUEUE_PAGE
    page = max(0, min(page, pages - 1))
    lines = [f"📋 Your queue - {len(items)} posts (page {page + 1} of {pages})",
             "Tap a number to read, edit or delete that post.", ""]
    row = []
    for number, it in enumerate(items[page * QUEUE_PAGE:(page + 1) * QUEUE_PAGE],
                                start=page * QUEUE_PAGE + 1):
        tag = (f"📌 {fmt_dt(local_from_utc_str(it['scheduled_for']))}" if it["scheduled_for"]
               else it["category"])
        lines.append(f"{number}. {tag} - {one_line(it['text'], 60)}"
                     + ("  📷" if it["photo_file_id"] else ""))
        row.append(btn(str(number), f"qv:{it['id']}"))
    kb = types.InlineKeyboardMarkup()
    kb.row(*row)
    nav = []
    if page > 0:
        nav.append(btn("◀ Previous", f"qlist:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("Next ▶", f"qlist:{page + 1}"))
    if nav:
        kb.row(*nav)
    return "\n".join(lines), kb, page


def send_queue_list(uid, page=0, edit_message=None):
    text, kb, _ = render_queue_list(uid, page)
    if text is None:
        bot.send_message(uid, "Your queue is empty - /add to feed me posts.")
        return
    if edit_message is not None:
        try:
            bot.edit_message_text(text, edit_message.chat.id, edit_message.message_id, reply_markup=kb)
            return
        except Exception:
            pass                       # fall back to a fresh message
    bot.send_message(uid, text, reply_markup=kb)


def leave_queue_edit(uid):
    """If she was in the middle of typing a replacement, drop back to where she was."""
    st = get_state(uid)
    if st.get("stage") == "editing_queue_item":
        st["stage"] = st.pop("return_stage", None)
    st.pop("edit_item", None)


def _my_queued_item(call):
    """The item a button points at, but only if it is hers and still waiting."""
    uid = call.from_user.id
    item = db.get_item(int(call.data.split(":", 1)[1]))
    if not item or item["uid"] != uid or item["status"] != "queued":
        bot.answer_callback_query(call.id, "That post isn't in your queue any more.")
        return None
    return item


def send_item_card(uid, item_id):
    item = db.get_item(item_id)
    if not item or item["uid"] != uid or item["status"] != "queued":
        bot.send_message(uid, "That post isn't in your queue any more.")
        return
    order = queue_order(uid)
    position = next((i for i, it in enumerate(order) if it["id"] == item_id), 0)
    if item["scheduled_for"]:
        where = f"📌 Pinned for {fmt_dt(local_from_utc_str(item['scheduled_for']))} ({TZ_LABEL})"
    else:
        where = f"#{position + 1} in line"
    extras = (" · 📷 photo" if item["photo_file_id"] else "") + (" · 🔗 link card" if item["link"] else "")
    if item["photo_file_id"]:
        try:
            bot.send_photo(uid, item["photo_file_id"])
        except Exception:
            log.warning("could not preview photo for item %s", item_id)
    kb = types.InlineKeyboardMarkup()
    kb.row(btn("✏️ Edit text", f"qe:{item_id}"), btn("🏷 Change category", f"qk:{item_id}"))
    kb.row(btn("🗑 Delete", f"qx:{item_id}"), btn("⬅ Back to list", f"qlist:{position // QUEUE_PAGE}"))
    bot.send_message(uid, f"{item['category']} · {where}{extras}\n\n{item['text']}"[:4000], reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("qlist:"))
def on_queue_list(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    leave_queue_edit(uid)
    bot.answer_callback_query(call.id)
    # the same message is reused for paging; from the summary it opens a new one
    send_queue_list(uid, int(call.data[6:]),
                    edit_message=call.message if call.message.text and
                    call.message.text.startswith("📋") else None)


@bot.callback_query_handler(func=lambda c: c.data.startswith("qv:"))
def on_item_view(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    leave_queue_edit(uid)
    item = _my_queued_item(call)
    if item:
        bot.answer_callback_query(call.id)
        send_item_card(uid, item["id"])


@bot.callback_query_handler(func=lambda c: c.data.startswith("qe:"))
def on_item_edit(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    item = _my_queued_item(call)
    if not item:
        return
    st = get_state(uid)
    if st.get("stage") != "editing_queue_item":
        st["return_stage"] = st.get("stage")
    st["stage"] = "editing_queue_item"
    st["edit_item"] = item["id"]
    bot.answer_callback_query(call.id)
    bot.send_message(uid, "✏️ Send me the new text for this post - it replaces the old wording. "
                          "(The category, photo and link stay as they are. Type cancel to keep it as it is.)")


def handle_item_edit_text(uid, text):
    st = get_state(uid)
    item_id = st.get("edit_item")
    if text.strip().lower() == "cancel":
        leave_queue_edit(uid)
        bot.send_message(uid, "No problem - left as it was.")
        return
    if len(text) < 5 or len(text) > 3000:
        bot.send_message(uid, "A post needs to be between 5 and 3,000 characters (LinkedIn's limit). "
                              "Try again, or type cancel.")
        return
    ok = db.update_item(uid, item_id, text=text)
    leave_queue_edit(uid)
    if ok:
        bot.send_message(uid, "✅ Updated.")
        send_item_card(uid, item_id)
    else:
        bot.send_message(uid, "That post isn't in your queue any more.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("qk:"))
def on_item_move(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    leave_queue_edit(uid)
    item = _my_queued_item(call)
    if not item:
        return
    get_state(uid)["move_item"] = item["id"]
    bot.answer_callback_query(call.id)
    ask_category(uid, f"Move this post out of {item['category']} - to which category?", "qm")


def do_move(uid, category):
    item_id = get_state(uid).pop("move_item", None)
    if item_id and db.update_item(uid, item_id, category=category):
        item = db.get_item(item_id)
        bot.send_message(uid, f"✅ Moved to {item['category']}.")
        send_item_card(uid, item_id)
    else:
        bot.send_message(uid, "I couldn't move that one - it may have been posted or removed.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("qm:"))
def on_item_move_pick(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    kind, name = _category_from_callback(call, "qm")
    bot.answer_callback_query(call.id)
    if kind == "new":
        ask_new_category(uid, "move")
    elif kind == "gone":
        bot.send_message(uid, "That list expired - open the post again and tap 🏷 Change category.")
    else:
        do_move(uid, name)


@bot.callback_query_handler(func=lambda c: c.data.startswith("qx:"))
def on_item_delete_from_card(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    leave_queue_edit(uid)
    item = _my_queued_item(call)
    if not item:
        return
    db.delete_item(uid, item["id"])
    bot.answer_callback_query(call.id, "Deleted 🗑")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    send_queue_list(uid, 0)


# ---------------------------------------------------------------- /schedule
def days_keyboard(selected):
    kb = types.InlineKeyboardMarkup()
    labels = [("✅ " if i in selected else "") + store_mod.DAY_LABELS[i] for i in range(7)]
    kb.row(*[btn(labels[i], f"sd:{i}") for i in range(0, 4)])
    kb.row(*[btn(labels[i], f"sd:{i}") for i in range(4, 7)])
    kb.row(btn("Weekdays", "sdw"), btn("Every day", "sde"))
    kb.row(btn("Next ▶ pick a time", "sdn"))
    return kb


def time_keyboard(half=False, prefix="st"):
    """prefix "st" = picking a weekly slot, "pt" = picking the time for one post."""
    kb = types.InlineKeyboardMarkup(row_width=4)
    minute = "30" if half else "00"
    hours = range(10, 22) if half else range(10, 23)        # 10:30-21:30 / 10:00-22:00
    kb.add(*[btn(store_mod.fmt_time(f"{h:02d}:{minute}").replace(":00 ", " "), f"{prefix}:{h:02d}{minute}")
             for h in hours])
    kb.row(btn("🕙 On the hour" if half else "🕧 Half-hours", f"{prefix[0]}th:00" if half else f"{prefix[0]}th:30"))
    return kb


def send_schedule_view(uid):
    slots = db.list_slots(uid)
    paused = db.is_paused(uid)
    lines = [f"📅 Your posting schedule ({TZ_LABEL})", ""]
    if slots:
        by_day = {}
        for s in slots:
            by_day.setdefault(s["dow"], []).append(s["hhmm"])
        for dow in sorted(by_day):
            lines.append(f"{store_mod.DAY_LABELS[dow]}: "
                         + ", ".join(store_mod.fmt_time(h) for h in sorted(by_day[dow])))
    else:
        lines.append("No posting times yet. Tap ➕ Add times to choose your days and times.")
    lines += ["", "Status: " + ("⏸ paused" if paused else "▶ active"),
              "Queue: " + counts_line(uid)]
    if slots:
        lines.append(describe_next(uid))
    kb = types.InlineKeyboardMarkup()
    kb.row(*([btn("➕ Add times", "sadd")] + ([btn("🗑 Remove a time", "srm")] if slots else [])))
    if slots:
        kb.row(btn("▶ Resume" if paused else "⏸ Pause scheduling", "sresume" if paused else "spause"))
    bot.send_message(uid, "\n".join(lines), reply_markup=kb)


def begin_add_slots(uid):
    get_state(uid)["sched_days"] = set()
    bot.send_message(uid, f"Which days do you want to post? Tick one or more, then tap Next.\n"
                          f"(Posting times run from 10 AM to 10 PM, {TZ_LABEL}.)",
                     reply_markup=days_keyboard(set()))


@bot.message_handler(commands=["schedule"])
def cmd_schedule(message):
    if guard(message):
        send_schedule_view(message.from_user.id)


@bot.callback_query_handler(func=lambda c: c.data == "sadd")
def on_sched_add(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    bot.answer_callback_query(call.id)
    begin_add_slots(uid)


def _refresh_days(call, selected):
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=days_keyboard(selected))
    except Exception:
        pass        # "message is not modified" when nothing changed


@bot.callback_query_handler(func=lambda c: c.data.startswith("sd:"))
def on_sched_day(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    selected = get_state(uid).setdefault("sched_days", set())
    day = int(call.data[3:])
    selected.symmetric_difference_update({day})
    bot.answer_callback_query(call.id)
    _refresh_days(call, selected)


@bot.callback_query_handler(func=lambda c: c.data in ("sdw", "sde"))
def on_sched_day_group(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    selected = get_state(uid).setdefault("sched_days", set())
    selected.clear()
    selected.update(range(5) if call.data == "sdw" else range(7))
    bot.answer_callback_query(call.id)
    _refresh_days(call, selected)


@bot.callback_query_handler(func=lambda c: c.data == "sdn")
def on_sched_days_next(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    selected = get_state(uid).get("sched_days") or set()
    if not selected:
        bot.answer_callback_query(call.id, "Tick at least one day first.")
        return
    bot.answer_callback_query(call.id)
    names = ", ".join(store_mod.DAY_LABELS[d] for d in sorted(selected))
    bot.send_message(uid, f"What time on {names}? Pick one (10 AM - 10 PM, {TZ_LABEL}).\n"
                          f"Need something else, like 3:20 PM? Just tell me in chat.",
                     reply_markup=time_keyboard())


@bot.callback_query_handler(func=lambda c: c.data.startswith("sth:"))
def on_sched_time_grid(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=time_keyboard(half=call.data == "sth:30"))
    except Exception:
        pass


def add_slots(uid, days, hhmm):
    """Create weekly slots (days = weekday indexes) and report back. Shared by the
    buttons and by chat. Returns a one-line summary."""
    now = datetime.now(TZ)
    added = [store_mod.DAY_LABELS[d] for d in days if db.add_slot(uid, d, hhmm, now_local=now)]
    had = [store_mod.DAY_LABELS[d] for d in days if store_mod.DAY_LABELS[d] not in added]
    msg = f"✅ Added {', '.join(added)} at {store_mod.fmt_time(hhmm)} ({TZ_LABEL})." if added \
        else f"You already had {store_mod.fmt_time(hhmm)} on those days."
    if added and had:
        msg += f"\n(You already had: {', '.join(had)}.)"
    bot.send_message(uid, msg + f"\n{describe_next(uid)}")
    return msg


@bot.callback_query_handler(func=lambda c: c.data.startswith("st:"))
def on_sched_time(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    raw = call.data[3:]
    hhmm = f"{raw[:2]}:{raw[2:]}"
    days = sorted(get_state(uid).get("sched_days") or [])
    if not days:
        bot.answer_callback_query(call.id, "Pick your days first - tap /schedule.")
        return
    if not store_mod.in_window(hhmm):
        bot.answer_callback_query(call.id, "I only post between 10 AM and 10 PM.")
        return
    bot.answer_callback_query(call.id, "Saved ✅")
    get_state(uid).pop("sched_days", None)
    add_slots(uid, days, hhmm)
    send_schedule_view(uid)


@bot.callback_query_handler(func=lambda c: c.data == "srm")
def on_sched_remove_menu(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    slots = db.list_slots(uid)
    bot.answer_callback_query(call.id)
    if not slots:
        bot.send_message(uid, "You don't have any posting times to remove.")
        return
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[btn(f"🗑 {store_mod.fmt_slot(s['dow'], s['hhmm'])}", f"srmd:{s['id']}") for s in slots])
    bot.send_message(uid, "Tap a time to remove it:", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("srmd:"))
def on_sched_remove(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    ok = db.remove_slot_id(uid, int(call.data[5:]))
    bot.answer_callback_query(call.id, "Removed 🗑" if ok else "Already gone")
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    send_schedule_view(uid)


@bot.callback_query_handler(func=lambda c: c.data in ("spause", "sresume"))
def on_sched_pause(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    db.set_paused(uid, call.data == "spause")
    bot.answer_callback_query(call.id, "Paused ⏸" if call.data == "spause" else "Resumed ▶")
    send_schedule_view(uid)


# ================================================================ THE SCHEDULER
# A background thread wakes every 30 seconds. When a person's slot time arrives it
# posts their next queued item (alternating categories) and tells them how it went.
SCHEDULER_INTERVAL = 30
PINNED_MAX_LATE_MINUTES = 120


def scheduler_tick(now_utc=None, now_local=None):
    now_utc = now_utc or store_mod.utcnow()
    now_local = now_local or datetime.now(TZ)

    # one-off posts pinned to an exact time
    for item in db.expire_pinned(now_utc, PINNED_MAX_LATE_MINUTES):
        try:
            bot.send_message(item["uid"], "⚠️ A post pinned to a specific time was missed (the bot "
                                          f"was offline):\n“{one_line(item['text'], 90)}”\n"
                                          "It was NOT posted - open /queue to re-add it.")
        except Exception:
            log.exception("could not notify about missed pinned post")
    for item in db.due_pinned(now_utc, PINNED_MAX_LATE_MINUTES):
        ok, detail = post_queue_item(item)
        announce_result(item, ok, detail, "pinned time")

    # her weekly slots
    by_uid = {}
    for slot in db.due_slots(now_local):
        by_uid.setdefault(slot["uid"], []).append(slot)
    today = now_local.strftime("%Y-%m-%d")
    for uid, slots in by_uid.items():
        for slot in slots:                       # mark first: never fire the same slot twice
            db.mark_fired(slot["id"], today)
        label = store_mod.fmt_slot(slots[0]["dow"], slots[0]["hhmm"])
        item = db.next_item(uid)                 # one post per tick even if two slots caught up
        if item is None:
            try:
                bot.send_message(uid, f"⏰ It's {label}, but your queue is empty so nothing went out. "
                                      f"Send me more posts with /add.")
            except Exception:
                log.exception("could not notify %s about an empty queue", uid)
            continue
        ok, detail = post_queue_item(item)
        announce_result(item, ok, detail, f"scheduled for {label}")


def scheduler_loop():
    while True:
        try:
            scheduler_tick()
        except Exception:
            log.exception("scheduler tick failed")
        time.sleep(SCHEDULER_INTERVAL)


def start_scheduler():
    # a post that was mid-flight when the bot stopped may or may not have gone out -
    # say so rather than guess and risk posting twice
    for item in db.recover_interrupted():
        try:
            bot.send_message(item["uid"], "⚠️ The bot restarted while it was posting this:\n"
                                          f"“{one_line(item['text'], 90)}”\n"
                                          "Check your LinkedIn first - if it didn't go out, open "
                                          "/queue and re-add it.")
        except Exception:
            log.exception("could not notify about an interrupted post")
    threading.Thread(target=scheduler_loop, name="scheduler", daemon=True).start()


# ================================================================ CHAT (free text)
# Each tool is something the brain (brain.py) can decide she meant. The brain only
# chooses; the code below validates and does the real work.

def remember(uid, role, text):
    history = get_state(uid).setdefault("history", [])
    history.append({"role": role, "content": (text or "")[:1500]})
    del history[:-12]


def brain_context(uid):
    st = get_state(uid)
    now = datetime.now(TZ)
    return {
        "today": now.strftime("%A %Y-%m-%d"),
        "time_now": now.strftime("%H:%M"),
        "timezone": TZ_NAME,
        "posting_window": "10:00-22:00",
        "has_draft": bool(st.get("draft")),
        "draft": (st.get("draft") or "")[:1500],
        "options_on_screen": len(st.get("options") or []) if st.get("stage") == "choosing" else 0,
        "queue_counts": db.counts(uid),
        "pinned_posts": len(db.list_queued(uid, pinned=True)),
        "weekly_schedule": [store_mod.fmt_slot(s["dow"], s["hhmm"]) for s in db.list_slots(uid)],
        "schedule_paused": db.is_paused(uid),
        "known_categories": category_choices(uid),
        "in_bulk_add_mode": st.get("stage") == "bulk_add",
    }


def _days_from(args):
    return [store_mod.DAYS.index(d) for d in (args.get("days") or []) if d in store_mod.DAYS]


def tool_show_queue(uid, args):
    send_queue_view(uid)
    return "showed the queue"


def tool_show_schedule(uid, args):
    send_schedule_view(uid)
    return "showed the schedule"


def tool_add_schedule_slots(uid, args):
    days = _days_from(args)
    hhmm = store_mod.parse_hhmm(args.get("time"))
    if not days or not hhmm:
        bot.send_message(uid, "Which days, and what time? (Anything from 10 AM to 10 PM.)")
        return "asked which days/time"
    if not store_mod.in_window(hhmm):
        bot.send_message(uid, f"I can only post between 10 AM and 10 PM - "
                              f"{store_mod.fmt_time(hhmm)} is outside that. Pick a time in the window?")
        return "time outside window, asked again"
    return add_slots(uid, days, hhmm)


def tool_remove_schedule_slots(uid, args):
    days = _days_from(args)
    hhmm = store_mod.parse_hhmm(args.get("time")) if args.get("time") else None
    if not days and not hhmm:
        bot.send_message(uid, "Which time should I remove? Tap /schedule and use 🗑 Remove a time, "
                              "or tell me the day and time.")
        return "asked which slot to remove"
    n = db.remove_slots(uid, days or None, hhmm)
    bot.send_message(uid, f"🗑 Removed {n} posting time{'s' if n != 1 else ''}." if n
                     else "I couldn't find a posting time matching that.")
    send_schedule_view(uid)
    return f"removed {n} slots"


def tool_set_schedule_paused(uid, args):
    paused = bool(args.get("paused"))
    db.set_paused(uid, paused)
    bot.send_message(uid, "⏸ Scheduling paused - nothing will post until you say resume."
                     if paused else "▶ Scheduling is back on.")
    return "paused" if paused else "resumed"


def tool_add_draft_to_queue(uid, args):
    category = (args.get("category") or "").strip()
    if not category:
        ask_category(uid, "Which category is this post?", "qc")
        return "asked for a category"
    queue_current_draft(uid, category)
    return f"queued the draft under {category}"


def tool_schedule_draft_at(uid, args):
    if not get_state(uid).get("draft"):
        bot.send_message(uid, "I don't have a draft open to schedule. Make one first.")
        return "no draft"
    try:
        day = datetime.strptime(str(args.get("date")), "%Y-%m-%d")
    except ValueError:
        bot.send_message(uid, "Which date? (for example 'this Friday' or '24 Oct')")
        return "asked for a date"
    hhmm = store_mod.parse_hhmm(args.get("time"))
    if not hhmm:
        bot.send_message(uid, "What time? (10 AM - 10 PM)")
        return "asked for a time"
    if not store_mod.in_window(hhmm):
        bot.send_message(uid, "I can only post between 10 AM and 10 PM - pick a time in that window?")
        return "time outside window"
    when = datetime(day.year, day.month, day.day, int(hhmm[:2]), int(hhmm[3:]), tzinfo=TZ)
    now = datetime.now(TZ)
    if when <= now + timedelta(minutes=2):
        bot.send_message(uid, "That time has already passed (or is too close). Pick a later time?")
        return "time in the past"
    if when > now + timedelta(days=90):
        bot.send_message(uid, "I can only schedule up to 90 days ahead.")
        return "too far ahead"
    category = db.canonical_category(uid, args.get("category") or "General")
    queue_current_draft(uid, category, scheduled_for=when.astimezone(timezone.utc).replace(tzinfo=None))
    return f"pinned the draft for {fmt_dt(when)}"


def tool_update_draft(uid, args):
    new_text = (args.get("new_text") or "").strip()
    st = get_state(uid)
    if not new_text:
        return "nothing to update"
    st["draft"] = new_text
    st["stage"] = "editing"
    send_draft(uid)
    return "updated the draft"


def tool_write_post_about(uid, args):
    topic = (args.get("topic") or "").strip()
    if len(topic) < 3:
        bot.send_message(uid, "What should the post be about?")
        return "asked for a topic"
    generate_opinions(uid, topic)
    return f"wrote options about: {one_line(topic, 60)}"


def tool_start_bulk_add(uid, args):
    begin_bulk(uid, args.get("category"))
    return "started bulk add"


def tool_pick_option(uid, args):
    try:
        number = int(args.get("number"))
    except (TypeError, ValueError):
        return "no option number"
    if not select_option(uid, number - 1):
        bot.send_message(uid, "I don't see that option on screen any more.")
        return "option not available"
    return f"loaded option {number} as the draft"


def tool_confirm_post_now(uid, args):
    if not get_state(uid).get("draft"):
        bot.send_message(uid, "There's nothing ready to post yet - make a draft first.")
        return "no draft"
    bot.send_message(uid, "Here's your draft - tap ✅ when you're ready and I'll post it.")
    send_draft(uid)
    return "showed the confirm button"


TOOL_HANDLERS = {
    "show_queue": tool_show_queue,
    "show_schedule": tool_show_schedule,
    "add_schedule_slots": tool_add_schedule_slots,
    "remove_schedule_slots": tool_remove_schedule_slots,
    "set_schedule_paused": tool_set_schedule_paused,
    "add_draft_to_queue": tool_add_draft_to_queue,
    "schedule_draft_at": tool_schedule_draft_at,
    "update_draft": tool_update_draft,
    "write_post_about": tool_write_post_about,
    "start_bulk_add": tool_start_bulk_add,
    "pick_option": tool_pick_option,
    "confirm_post_now": tool_confirm_post_now,
}


def converse(uid, text):
    """Let the brain interpret a free-text message. True if it handled it, False if
    the brain is off or failed (the caller then falls back to the plain behaviour)."""
    if not brain.enabled:
        return False
    bot.send_chat_action(uid, "typing")
    try:
        decision = brain.interpret(text, brain_context(uid), get_state(uid).get("history", []))
    except Exception:
        log.exception("brain failed")
        return False
    remember(uid, "user", text)
    tool, args = decision["tool"], decision.get("args") or {}
    handler = TOOL_HANDLERS.get(tool)
    if handler is None:
        reply = (args.get("text") or "…")[:4000]
        bot.send_message(uid, reply)
        remember(uid, "assistant", reply)
        return True
    try:
        summary = handler(uid, args)
    except Exception:
        log.exception("tool %s failed", tool)
        bot.send_message(uid, "Sorry, something went wrong doing that. Try the buttons "
                              "(/schedule or /queue), or say it a different way.")
        return True
    remember(uid, "assistant", f"[{tool}] {summary or ''}")
    return True


# ---------------------------------------------------------------- catch-all text
# Keep LAST so /start, /post, /queue, /schedule, /add... are handled first.
@bot.message_handler(func=lambda m: True, content_types=["text"])
def on_text(message):
    uid = message.from_user.id
    if not is_owner(uid):
        bot.reply_to(message, "This bot is private.")
        return
    st = get_state(uid)
    stage = st.get("stage")
    text = (message.text or "").strip()

    # she was asked to name a new category
    if stage == "awaiting_category_name":
        handle_category_name(uid, text)
        return

    # she tapped ✏️ Edit text on a queued post: this message is the new wording
    if stage == "editing_queue_item":
        handle_item_edit_text(uid, text)
        return

    # feeding the queue: every message is a finished post
    if stage == "bulk_add":
        if text.lower() in ("done", "finish", "finished", "stop"):
            finish_bulk(uid)
        else:
            bulk_queue_text(uid, message)
        return

    # Caption flow: she's describing a photo, or looking at caption options. Notes
    # always regenerate; short messages while options are showing may be chat
    # ("I like the second one") when the brain is on.
    if st.get("mode") == "caption" and st.get("image_url") \
            and stage in ("awaiting_keywords", "choosing"):
        if stage == "choosing" and len(text) < 80 and converse(uid, text):
            return
        generate_captions(uid, text)
        return

    # A draft is open: long / multi-line text is her edited version of the post. A
    # short message might be an instruction ("make it shorter", "queue it").
    if stage == "editing":
        if len(text) < 220 and "\n" not in text and converse(uid, text):
            return
        st["draft"] = text
        send_draft(uid)
        return

    # Otherwise: a link or a long paste is an article to comment on; anything short
    # is conversation.
    url = extract_url(message)
    bare_link = bool(url) and len(text.replace(url, "").strip()) < 80
    if not bare_link and len(text) < 400 and converse(uid, text):
        return
    content, url, title, desc, image = resolve_opinion_source(uid, message)
    if not content or len(content.strip()) < 10:
        bot.reply_to(message, "Send me an article link (any site), or forward/paste the "
                              "text, and I'll suggest 3 angles. Or send a photo to caption.\n\n"
                              "/help shows everything I can do.")
    else:
        generate_opinions(uid, content, url, title, desc, image)


if __name__ == "__main__":
    log.info("Assistant running (database: %s, timezone: %s, chat brain: %s)",
             DB_PATH, TZ_NAME, "on" if brain.enabled else "off - set OPENAI_API_KEY to enable")
    start_scheduler()
    bot.infinity_polling(skip_pending=True)
