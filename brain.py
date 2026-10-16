"""
Kaypoh bot - the conversation brain
-----------------------------------
Lets people just TALK to the bot ("schedule this for Friday 7pm", "make it
shorter", "what's in my queue?", "how do I add more posts?") instead of only
tapping buttons.

How it works: each free-text message goes to an OpenAI chat model together with
a snapshot of where she is (draft open? queue? schedule?). The model either
answers normally, like ChatGPT, or calls ONE of the tools below. assistant.py
then does the real work (it validates times, touches the database, etc.) - the
model only decides WHAT she meant, it never posts anything itself.

Setup (on the VM, one time):
    OPENAI_API_KEY=sk-...        # needed for this to switch on
    BOT_MODEL=gpt-4.1-mini       # optional - any chat model that supports tools

This is separate from the model inside Make: Make only drafts the opinion /
caption options. Without OPENAI_API_KEY the bot still works exactly as before
(buttons + forwarding); it just can't chat.
"""

import json
import logging
import os

log = logging.getLogger("kaypoh.brain")

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _tool(name, description, properties=None, required=None):
    return {"type": "function", "function": {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties or {},
                       "required": required or [], "additionalProperties": False},
    }}


TOOLS = [
    _tool("show_queue",
          "Show what is waiting in her content queue and which posts go out next."),
    _tool("show_schedule",
          "Show her weekly posting schedule (the days and times posts go out)."),
    _tool("add_schedule_slots",
          "Add weekly posting time(s). Posts only go out between 10:00 and 22:00.",
          {"days": {"type": "array", "items": {"type": "string", "enum": _DAYS},
                    "description": "Which weekdays."},
           "time": {"type": "string", "description": "24-hour HH:MM, e.g. 19:00."}},
          ["days", "time"]),
    _tool("remove_schedule_slots",
          "Remove weekly posting time(s). Give days, a time, or both.",
          {"days": {"type": "array", "items": {"type": "string", "enum": _DAYS}},
           "time": {"type": "string", "description": "24-hour HH:MM."}}),
    _tool("set_schedule_paused",
          "Pause or resume ALL scheduled posting (e.g. she is on holiday).",
          {"paused": {"type": "boolean"}}, ["paused"]),
    _tool("add_draft_to_queue",
          "Put the draft she is working on into the queue under a category such as "
          "Finance or Lifestyle. Only call this when you know the category - if she has "
          "not said, ask her first.",
          {"category": {"type": "string"}}, ["category"]),
    _tool("schedule_draft_at",
          "Pin the draft she is working on to ONE specific date and time (a one-off, "
          "separate from the weekly rotation). Needs a category too - ask if missing.",
          {"date": {"type": "string", "description": "YYYY-MM-DD, worked out from today's date."},
           "time": {"type": "string", "description": "24-hour HH:MM between 10:00 and 22:00."},
           "category": {"type": "string"}},
          ["date", "time", "category"]),
    _tool("update_draft",
          "Replace the draft with new text. Use it (a) when she asks for a change - "
          "shorter, funnier, more formal, add something, fix something - by writing the "
          "FULL revised post yourself, or (b) when her short message is clearly her own "
          "edited version of the post.",
          {"new_text": {"type": "string", "description": "The complete new post text."}},
          ["new_text"]),
    _tool("write_post_about",
          "She describes a topic or idea (no article) and wants post options written about it.",
          {"topic": {"type": "string"}}, ["topic"]),
    _tool("start_bulk_add",
          "She wants to feed in several finished posts at once. If she named a category, "
          "pass it; otherwise leave it out and the bot will ask.",
          {"category": {"type": "string"}}),
    _tool("pick_option",
          "Choose one of the options currently on screen (POV / caption) by its number.",
          {"number": {"type": "integer", "minimum": 1}}, ["number"]),
    _tool("confirm_post_now",
          "She wants to publish the current draft right now. This does NOT post - it just "
          "shows the confirm button for her to tap."),
]


SYSTEM_PROMPT = """You are Kaypoh, a friendly assistant inside Telegram that helps a Singapore insurance & wealth advisor create and schedule LinkedIn posts.

What the bot can do (ALWAYS use the matching tool - never claim an action happened unless you called its tool):
- Turn a forwarded article / link / topic into 3 post options, or a photo + notes into caption options
- Rewrite the draft she is working on when she asks for changes
- Keep a content QUEUE of finished posts, each tagged with a category (e.g. Finance, Lifestyle)
- Run a weekly SCHEDULE: she picks days and times (any day, 10:00 to 22:00 only); at each time the bot posts the next queued item, alternating between categories so her feed stays balanced
- Pin one draft to a specific date and time
- Pause / resume all scheduled posting

How to behave:
- Talk like a warm, capable colleague. Short messages - this is Telegram. Plain text, no markdown headings or bullet-heavy essays.
- If she is just chatting or asking a question (including how this bot works or what to post about), answer naturally, like ChatGPT would. Do not call a tool for that.
- If her message looks like a news headline, an article blurb, or an idea / topic for a post, call write_post_about with it.
- If something you need is missing (which category, which day, which time), ask ONE short question instead of guessing.
- Posting window is 10:00-22:00. If she asks for a time outside it, say so and suggest the nearest allowed time.
- Nothing is posted to LinkedIn at the moment she asks - she must tap the green button herself. Scheduled posts go out at their time.
- Resolve words like "tomorrow" or "this Friday" using today's date from the state.
- When you rewrite a post: keep her warm, human voice; short paragraphs (max 2 lines each) with a blank line between them; never start a line with a space; don't force a question at the end; never invent facts, names or numbers.
- Don't repeat the whole draft back in chat; when you update it, the bot shows it for you."""


class Brain:
    def __init__(self):
        self.model = os.environ.get("BOT_MODEL", "gpt-4.1-mini")
        self.client = None
        key = os.environ.get("OPENAI_API_KEY")
        if key:
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=key, timeout=30)
            except Exception:
                log.exception("Could not start the OpenAI client - chat is switched off")

    @property
    def enabled(self):
        return self.client is not None

    def interpret(self, text, context, history):
        """-> {"tool": name, "args": {...}, "say": "..."}.
        A plain conversational answer comes back as tool == "reply"."""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": "Current state:\n" + json.dumps(context, ensure_ascii=False)},
            *history[-10:],
            {"role": "user", "content": text},
        ]
        kwargs = dict(model=self.model, messages=messages, tools=TOOLS,
                      tool_choice="auto", parallel_tool_calls=False)
        try:
            resp = self.client.chat.completions.create(temperature=0.4, **kwargs)
        except Exception as e:
            # some models (e.g. reasoning models) reject a custom temperature
            if "temperature" not in str(e).lower():
                raise
            resp = self.client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        if msg.tool_calls:
            call = msg.tool_calls[0]
            try:
                args = json.loads(call.function.arguments or "{}")
            except ValueError:
                args = {}
            return {"tool": call.function.name, "args": args, "say": (msg.content or "").strip()}
        return {"tool": "reply", "args": {"text": (msg.content or "").strip() or "…"}, "say": ""}
