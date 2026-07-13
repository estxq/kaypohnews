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
                                  "image_url": None, "mode": None, "article_url": None})


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


# ---------------------------------------------------------------- Make helpers
def ask_make_for_suggestions(mode, content):
    """mode = 'opinion' or 'caption'. Returns a list of option strings."""
    r = requests.post(MAKE_SUGGEST_URL, json={"mode": mode, "content": content}, timeout=60)
    r.raise_for_status()
    return r.json().get("options", [])

def publish_to_linkedin(mode, text, image_url=None):
    """mode = 'text' or 'image'. Returns Make's JSON response."""
    r = requests.post(MAKE_PUBLISH_URL,
                      json={"mode": mode, "text": text, "image_url": image_url},
                      timeout=60)
    r.raise_for_status()
    return r.json()

def show_options(uid, header, options, label):
    """Send the header, then each option as its OWN message with a bold
    "<label> N" title (e.g. POV 1) and its own pick button. Separate bubbles
    keep long, LinkedIn-length drafts easy to tell apart and to copy."""
    bot.send_message(uid, f"<b>{html.escape(header, quote=False)}</b>", parse_mode="HTML")
    for i, o in enumerate(options):
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(f"✍️ Use {label} {i + 1}", callback_data=f"pick:{i}"))
        bot.send_message(
            uid,
            f"<b>{label} {i + 1}</b>\n\n{html.escape(o, quote=False)}",
            reply_markup=kb,
            parse_mode="HTML",
        )

def draft_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Post to LinkedIn", callback_data="do_post"))
    return kb

def send_draft(uid):
    """Show the draft as its OWN clean message (just the post text, nothing
    else) so a long-press -> Copy grabs exactly the caption, then a separate
    message with the how-to and the ✅ button."""
    st = get_state(uid)
    bot.send_message(uid, st["draft"])   # clean, copy-friendly — no extra text
    link_note = "\n🔗 The article link will be added to the post automatically." \
        if st.get("article_url") else ""
    bot.send_message(
        uid,
        "👆 That's your draft.\n\n"
        "✏️ To edit: copy it, tweak, and send it back (it replaces the draft) — "
        "or just type a fresh version.\n"
        "Tap ✅ when you're ready."
        f"{link_note}",
        reply_markup=draft_keyboard(),
    )

def do_publish(uid, notify):
    """notify(text) sends feedback back to her. Shared by the button and /post."""
    st = get_state(uid)
    if not st.get("draft"):
        notify("No draft yet. Forward an article, pick a perspective, or send a photo first.")
        return
    publish_mode = "image" if st.get("image_url") else "text"
    # Cite the source article on opinion posts so readers see what she's reacting to.
    text = st["draft"]
    if publish_mode == "text" and st.get("article_url"):
        text = f"{text}\n\n{st['article_url']}"
    notify("Posting to LinkedIn…")
    try:
        result = publish_to_linkedin(publish_mode, text, st.get("image_url"))
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


# ---------------------------------------------------------------- NEWS: article forwarded
@bot.message_handler(func=lambda m: m.forward_date is not None, content_types=["text"])
def on_forwarded_article(message):
    uid = message.from_user.id
    if not is_owner(uid):
        bot.reply_to(message, "This bot is private.")
        return
    article = message.text or message.caption or ""
    if not article:
        bot.reply_to(message, "Couldn't read any text from that — try forwarding the original post.")
        return
    bot.send_chat_action(uid, "typing")
    try:
        options = ask_make_for_suggestions("opinion", article)
    except Exception as e:
        bot.reply_to(message, f"Sorry, couldn't get suggestions: {e}")
        return
    st = get_state(uid)
    st.update(mode="opinion", options=options, image_url=None, draft=None,
              stage="choosing", article_url=extract_url(message))
    show_options(uid, "Here are some angles you could post:", options, "POV")


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
    get_state(uid).update(options=options, stage="choosing")
    show_options(uid, "Here are some caption ideas:", options, "Caption")
    bot.send_message(uid, "💡 Not quite right? Just send your notes again (tweaked) for a fresh set.")


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
        bot.send_message(uid, "Updated ✅")
        send_draft(uid)

    else:
        bot.reply_to(message, "Forward me a news article to comment on, or send me a photo to caption.")


if __name__ == "__main__":
    print("Assistant running… press Ctrl+C to stop.")
    bot.infinity_polling(skip_pending=True)
