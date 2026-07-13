#!/usr/bin/env python3
"""
Kaypoh LinkedIn Assistant  (the always-on half)
------------------------------------------------
Runs continuously (e.g. on a free Google Cloud e2-micro VM) using the SAME bot
token as news_bot.py. news_bot.py only *sends* the news; this script only
*listens* and replies, so they never conflict.

Two flows
  1) NEWS  -> she forwards a news post (from the channel, or anywhere) to this
     bot in their private chat. This script reads the forwarded text, asks
     Make (OpenAI) for 3 opinion angles, and shows them as options. She picks
     one, edits it by typing, then taps the "✅ Post to LinkedIn" button.
  2) PHOTO -> she sends a photo to the bot in a private chat, gives keywords,
     gets caption options, picks one, edits, taps "✅ Post to LinkedIn".

Make.com does the AI suggestions and the LinkedIn posting (two webhooks).

Setup
  pip install pyTelegramBotAPI requests
  export TELEGRAM_BOT_TOKEN="same token as news_bot.py"
  export MAKE_SUGGEST_URL="Make webhook that returns {'options':[...]}"
  export MAKE_PUBLISH_URL="Make webhook that posts to LinkedIn"
  export OWNER_TELEGRAM_ID="allowed numeric Telegram user id(s), comma-separated
                            e.g. 111111111 or 111111111,222222222 (her + tester)"
  python assistant.py

Both flows start with her messaging the bot directly (forwarding a post, or
sending a photo), so Telegram always allows the reply - no need to press
Start first, though /start still gives a quick how-to.

SECURITY: OWNER_TELEGRAM_ID locks every flow to her user id only, since the
Make webhooks are wired to one specific LinkedIn account.
"""

import os
import html
import requests
import telebot
from telebot import types

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]   # SAME token as news_bot.py
MAKE_SUGGEST_URL   = os.environ["MAKE_SUGGEST_URL"]     # returns AI options
MAKE_PUBLISH_URL   = os.environ["MAKE_PUBLISH_URL"]     # posts to LinkedIn
# One or more allowed user ids, comma-separated (e.g. "111,222" for her + a tester).
OWNER_TELEGRAM_IDS = {int(x) for x in os.environ["OWNER_TELEGRAM_ID"].split(",") if x.strip()}

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

def is_owner(uid):
    return uid in OWNER_TELEGRAM_IDS

# State keyed by user id (same as their private chat id).
STATE = {}   # user_id -> dict(stage, draft, options, image_url, mode, article_url)

def get_state(uid):
    return STATE.setdefault(uid, {"stage": None, "draft": None, "options": None,
                                  "image_url": None, "mode": None, "article_url": None,
                                  "article_title": None, "article_desc": None,
                                  "source": None})


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


def fetch_article_meta(url, limit=4000):
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
        }
    except Exception:
        return None


def resolve_opinion_source(uid, message):
    """Decide what to comment on and gather link-card details.
    Returns (content, url, title, description). If she basically just sent a
    link, the article text is fetched for the AI; either way, when a link is
    present we grab its title/description for the LinkedIn preview card."""
    url = extract_url(message)
    text = (message.text or message.caption or "").strip()
    remainder = text.replace(url, "").strip() if url else text
    bare_link = bool(url) and len(remainder) < 80
    title = desc = None
    if url:
        if bare_link:
            bot.send_message(uid, "🔗 Reading the article…")
        meta = fetch_article_meta(url)
        if meta:
            title, desc = meta.get("title"), meta.get("description")
            if bare_link and meta.get("text"):
                return meta["text"], url, title, desc
        elif bare_link:
            bot.send_message(uid, "(Couldn't read that link — I'll use what you sent instead.)")
    return text, url, title, desc


# ---------------------------------------------------------------- Make helpers
def ask_make_for_suggestions(mode, content):
    """mode = 'opinion' or 'caption'. Returns a list of option strings."""
    r = requests.post(MAKE_SUGGEST_URL, json={"mode": mode, "content": content}, timeout=60)
    r.raise_for_status()
    return r.json().get("options", [])

def publish_to_linkedin(mode, text, image_url=None, link=None, link_title=None, link_desc=None):
    """mode = 'text' (plain), 'image' (photo) or 'article' (link with preview
    card). Returns Make's JSON response."""
    r = requests.post(MAKE_PUBLISH_URL,
                      json={"mode": mode, "text": text, "image_url": image_url,
                            "link": link, "link_title": link_title, "link_desc": link_desc},
                      timeout=60)
    r.raise_for_status()
    return r.json()

def show_options(uid, header, options, label):
    """Send the header, then each option as its OWN message with a bold
    "<label> N" title (e.g. POV 1) and its own pick button. Separate bubbles
    keep long, LinkedIn-length drafts easy to tell apart and to copy."""
    n = len(options)
    bot.send_message(
        uid,
        f"<b>{html.escape(header, quote=False)}</b>\n"
        f"({n} to choose from — scroll down 👇)",
        parse_mode="HTML",
    )
    for i, o in enumerate(options):
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(f"✍️ Use {label} {i + 1}", callback_data=f"pick:{i}"))
        bot.send_message(
            uid,
            f"<b>{label} {i + 1} of {n}</b>\n\n{html.escape(o, quote=False)}",
            reply_markup=kb,
            parse_mode="HTML",
        )
    # A regenerate button so she can get a whole fresh set without re-forwarding
    # the article or re-uploading the photo (the source is kept in state).
    regen_kb = types.InlineKeyboardMarkup()
    regen_kb.add(types.InlineKeyboardButton("🔄 Regenerate (fresh set)", callback_data="regen"))
    bot.send_message(uid, "Not quite right?", reply_markup=regen_kb)

def draft_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Post to LinkedIn", callback_data="do_post"))
    return kb

def send_draft(uid):
    """Show the draft as ONE clean message — just the post text, with the ✅
    button attached below it. No instruction words, so a long-press -> Copy
    grabs exactly the post and nothing else to delete. (For opinion posts the
    article link is still added automatically when she posts.)"""
    st = get_state(uid)
    bot.send_message(uid, st["draft"], reply_markup=draft_keyboard())

def do_publish(uid, notify):
    """notify(text) sends feedback back to her. Shared by the button and /post."""
    st = get_state(uid)
    if not st.get("draft"):
        notify("No draft yet. Forward an article, pick a perspective, or send a photo first.")
        return
    text = st["draft"]
    link = link_title = link_desc = None
    if st.get("image_url"):
        publish_mode = "image"
    elif st.get("article_url"):
        # Post as an article share so LinkedIn shows the preview card, instead
        # of gluing an ugly shortened link onto the text.
        publish_mode = "article"
        link = st["article_url"]
        link_title = (st.get("article_title") or "Read the article")[:400]
        link_desc = st.get("article_desc")
    else:
        publish_mode = "text"
    notify("Posting to LinkedIn…")
    try:
        result = publish_to_linkedin(publish_mode, text, st.get("image_url"),
                                     link, link_title, link_desc)
    except Exception as e:
        bot.send_message(uid, f"Posting failed: {e}")
        return
    url = result.get("url", "")
    bot.send_message(uid, "✅ Posted!" + (f"\n{url}" if url else ""))
    STATE.pop(uid, None)


# ---------------------------------------------------------------- /start
@bot.message_handler(commands=["start"])
def cmd_start(message):
    get_state(message.from_user.id)
    bot.reply_to(
        message,
        "Hi! I help you post to LinkedIn.\n\n"
        "• Forward me a news article you want to comment on, and I'll suggest angles.\n"
        "• Or send me a photo here and I'll help you write a caption.\n\n"
        "Pick a suggestion, type edits if you like, then tap ✅ Post to LinkedIn when ready.",
    )


# ---------------------------------------------------------------- NEWS: opinion helper
def generate_opinions(uid, content, article_url=None, article_title=None, article_desc=None):
    """Turn an article (forwarded, typed or pasted) into 3 opinion angles.
    Reused for the first go and for every regenerate."""
    bot.send_chat_action(uid, "typing")
    try:
        options = ask_make_for_suggestions("opinion", content)
    except Exception as e:
        bot.send_message(uid, f"Sorry, couldn't get suggestions: {e}")
        return
    get_state(uid).update(mode="opinion", options=options, image_url=None, draft=None,
                          stage="choosing", article_url=article_url,
                          article_title=article_title, article_desc=article_desc,
                          source=content)
    show_options(uid, "Here are some angles you could post:", options, "POV")


# ---------------------------------------------------------------- NEWS: article forwarded
@bot.message_handler(func=lambda m: m.forward_date is not None, content_types=["text"])
def on_forwarded_article(message):
    uid = message.from_user.id
    if not is_owner(uid):
        bot.reply_to(message, "This bot is private.")
        return
    content, url, title, desc = resolve_opinion_source(uid, message)
    if not content:
        bot.reply_to(message, "Couldn't read any text or link from that — try forwarding the original post.")
        return
    generate_opinions(uid, content, url, title, desc)


# ---------------------------------------------------------------- PHOTO flow
@bot.message_handler(content_types=["photo"])
def on_photo(message):
    uid = message.from_user.id
    if not is_owner(uid):
        bot.reply_to(message, "This bot is private.")
        return
    file_id = message.photo[-1].file_id           # highest resolution
    file_info = bot.get_file(file_id)
    image_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_info.file_path}"
    st = get_state(uid)
    st.update(mode="caption", image_url=image_url, stage="awaiting_keywords",
              options=None, draft=None, article_url=None)
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
@bot.callback_query_handler(func=lambda c: c.data.startswith("pick:"))
def on_pick(call):
    uid = call.from_user.id
    if not is_owner(uid):
        bot.answer_callback_query(call.id, "This bot is private.")
        return
    st = get_state(uid)
    idx = int(call.data.split(":", 1)[1])
    options = st.get("options") or []
    if idx >= len(options):
        bot.answer_callback_query(call.id, "That option expired, try again.")
        return
    st["draft"] = options[idx]
    st["stage"] = "editing"
    bot.answer_callback_query(call.id, "Loaded into your draft.")
    send_draft(uid)


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


# ---------------------------------------------------------------- catch-all text
# Keep LAST so /start and /post are handled first.
@bot.message_handler(func=lambda m: True, content_types=["text"])
def on_text(message):
    uid = message.from_user.id
    st = get_state(uid)
    stage = st.get("stage")

    # Caption flow: while she hasn't locked in a draft yet (still giving notes
    # or looking at options), any text is treated as (new) notes -> fresh
    # captions. This is what lets her keep regenerating by resending notes.
    if st.get("mode") == "caption" and st.get("image_url") \
            and stage in ("awaiting_keywords", "choosing"):
        generate_captions(uid, message.text)

    elif stage == "editing":
        st["draft"] = message.text
        send_draft(uid)

    else:
        # Any other text — a pasted link OR typed/pasted article text — auto-
        # generates 3 angles. A bare link gets the article fetched and read.
        content, url, title, desc = resolve_opinion_source(uid, message)
        if not content or len(content.strip()) < 10:
            bot.reply_to(message, "Send me an article link (any site), or forward/paste the "
                                  "text, and I'll suggest 3 angles. Or send a photo to caption.")
        else:
            generate_opinions(uid, content, url, title, desc)


if __name__ == "__main__":
    print("Assistant running… press Ctrl+C to stop.")
    bot.infinity_polling(skip_pending=True)
