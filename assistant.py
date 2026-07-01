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
  export OWNER_TELEGRAM_ID="her numeric Telegram user id (from @userinfobot)"
  python assistant.py

Both flows start with her messaging the bot directly (forwarding a post, or
sending a photo), so Telegram always allows the reply - no need to press
Start first, though /start still gives a quick how-to.

SECURITY: OWNER_TELEGRAM_ID locks every flow to her user id only, since the
Make webhooks are wired to one specific LinkedIn account.
"""

import os
import requests
import telebot
from telebot import types

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]   # SAME token as news_bot.py
MAKE_SUGGEST_URL   = os.environ["MAKE_SUGGEST_URL"]     # returns AI options
MAKE_PUBLISH_URL   = os.environ["MAKE_PUBLISH_URL"]     # posts to LinkedIn
OWNER_TELEGRAM_ID  = int(os.environ["OWNER_TELEGRAM_ID"])  # only this user may use the bot

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

def is_owner(uid):
    return uid == OWNER_TELEGRAM_ID

# State keyed by user id (same as their private chat id).
STATE = {}   # user_id -> dict(stage, draft, options, image_url, mode)

def get_state(uid):
    return STATE.setdefault(uid, {"stage": None, "draft": None,
                                  "options": None, "image_url": None, "mode": None})


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

def options_keyboard(options):
    kb = types.InlineKeyboardMarkup()
    for i, _ in enumerate(options):
        kb.add(types.InlineKeyboardButton(f"Use option {i + 1}", callback_data=f"pick:{i}"))
    return kb

def show_options(uid, header, options):
    text = header + "\n\n" + "\n\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    bot.send_message(uid, text, reply_markup=options_keyboard(options))

def draft_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Post to LinkedIn", callback_data="do_post"))
    return kb

def do_publish(uid, notify):
    """notify(text) sends feedback back to her. Shared by the button and /post."""
    st = get_state(uid)
    if not st.get("draft"):
        notify("No draft yet. Forward an article, pick a perspective, or send a photo first.")
        return
    publish_mode = "image" if st.get("image_url") else "text"
    notify("Posting to LinkedIn…")
    try:
        result = publish_to_linkedin(publish_mode, st["draft"], st.get("image_url"))
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
    st.update(mode="opinion", options=options, image_url=None, draft=None, stage="choosing")
    show_options(uid, "Here are some angles you could post:", options)


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
              options=None, draft=None)
    bot.reply_to(message, "Nice photo! Send me a few keywords about it and I'll suggest captions.")


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
    bot.send_message(
        uid,
        "Here's your draft:\n\n"
        f"{st['draft']}\n\n"
        "✏️ Type any edits to replace it, then tap ✅ when ready.",
        reply_markup=draft_keyboard(),
    )


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


# ---------------------------------------------------------------- catch-all text
# Keep LAST so /start and /post are handled first.
@bot.message_handler(func=lambda m: True, content_types=["text"])
def on_text(message):
    uid = message.from_user.id
    st = get_state(uid)
    stage = st.get("stage")

    if stage == "awaiting_keywords":
        bot.send_chat_action(uid, "typing")
        try:
            options = ask_make_for_suggestions("caption", message.text)
        except Exception as e:
            bot.reply_to(message, f"Sorry, couldn't get captions: {e}")
            return
        st.update(options=options, stage="choosing")
        show_options(uid, "Here are some caption ideas:", options)

    elif stage == "editing":
        st["draft"] = message.text
        bot.reply_to(message, "Updated your draft. Tap ✅ when ready, or keep editing.",
                     reply_markup=draft_keyboard())

    else:
        bot.reply_to(message, "Forward me a news article to comment on, or send me a photo to caption.")


if __name__ == "__main__":
    print("Assistant running… press Ctrl+C to stop.")
    bot.infinity_polling(skip_pending=True)
