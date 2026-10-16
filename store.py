"""
Kaypoh bot - persistence + scheduling rules
-------------------------------------------
Everything that must SURVIVE a restart lives here (a small SQLite file):

  queue  - finished posts waiting to go out, each tagged with a category
  slots  - her weekly posting times (e.g. Tue 7:00 PM)
  prefs  - per-person switches (currently just "paused")

There is no Telegram or network code in this file on purpose, so the rules
(alternating categories, "is this slot due?", the 10am-10pm window) can be
tested on their own.

How posting is decided
  * At each slot time the bot posts ONE queued item.
  * Categories alternate: the category that was posted LEAST recently goes next
    (a category that has never been posted counts as "longest ago"), then the
    oldest item inside that category. With Finance + Lifestyle that gives
    Finance, Lifestyle, Finance, Lifestyle... and it still works for 3+ categories.
  * An item can also be PINNED to one exact date/time. Pinned items ignore the
    rotation and are never picked by a recurring slot.
"""

import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]      # index == datetime.weekday()
DAY_LABELS = [d.title() for d in DAYS]

WINDOW_START = "10:00"      # earliest allowed posting time (10 AM)
WINDOW_END = "22:00"        # latest allowed posting time (10 PM), inclusive
GRACE_MINUTES = 15          # if the bot was busy/restarting, still fire a slot this late

DEFAULT_CATEGORIES = ["Finance", "Lifestyle"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uid           INTEGER NOT NULL,
    category      TEXT    NOT NULL,
    text          TEXT    NOT NULL,
    photo_file_id TEXT,
    link          TEXT,
    link_title    TEXT,
    link_desc     TEXT,
    link_image    TEXT,
    scheduled_for TEXT,                       -- UTC "YYYY-MM-DDTHH:MM:SS" when pinned, else NULL
    status        TEXT    NOT NULL DEFAULT 'queued',   -- queued | posting | posted | failed
    created_at    TEXT    NOT NULL,
    posted_at     TEXT,
    result_url    TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_uid_status ON queue (uid, status);

CREATE TABLE IF NOT EXISTS slots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uid        INTEGER NOT NULL,
    dow        INTEGER NOT NULL,              -- 0 = Monday ... 6 = Sunday
    hhmm       TEXT    NOT NULL,              -- local time, 24h "HH:MM"
    last_fired TEXT,                          -- local date "YYYY-MM-DD" it last fired
    UNIQUE (uid, dow, hhmm)
);

CREATE TABLE IF NOT EXISTS prefs (
    uid    INTEGER PRIMARY KEY,
    paused INTEGER NOT NULL DEFAULT 0
);
"""


# ------------------------------------------------------------------ time helpers
_TIME_RE = re.compile(r"^\s*(\d{1,2})(?:[:.](\d{2}))?\s*([ap])?\.?m?\.?\s*$", re.I)


def parse_hhmm(text):
    """'19:00', '7pm', '7:30 PM', '7.30pm' -> 'HH:MM' (24h). Anything else -> None."""
    if text is None:
        return None
    s = str(text).strip().lower()
    if s == "noon":
        return "12:00"
    m = _TIME_RE.match(s)
    if not m:
        return None
    hour, minute, meridiem = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem.lower() == "p" else 0)
    elif hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def in_window(hhmm):
    """True when the time sits inside the allowed posting window (10:00-22:00)."""
    return hhmm is not None and WINDOW_START <= hhmm <= WINDOW_END


def fmt_time(hhmm):
    """'19:30' -> '7:30 PM'."""
    hour, minute = int(hhmm[:2]), int(hhmm[3:])
    return f"{(hour % 12) or 12}:{minute:02d} {'PM' if hour >= 12 else 'AM'}"


def fmt_slot(dow, hhmm):
    return f"{DAY_LABELS[dow]} {fmt_time(hhmm)}"


def _utc_str(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def utcnow():
    return datetime.utcnow().replace(microsecond=0)


def category_key(name):
    return " ".join((name or "").split()).lower()


# ------------------------------------------------------------------ rotation rule
def plan_order(items, last_posted, n):
    """Return up to n items in the order they would be posted.

    items        queued, un-pinned items (dicts with 'id' and 'category')
    last_posted  {category_key: iso timestamp of its latest post} (missing = never)

    Rule: next category = the one posted LEAST recently; inside it, oldest item.
    Used both to pick the real next item and to preview "up next" in /queue, so
    the preview can never disagree with what actually gets posted.
    """
    by_cat = {}
    for item in sorted(items, key=lambda i: i["id"]):
        by_cat.setdefault(category_key(item["category"]), []).append(item)
    recency = dict(last_posted)
    order = []
    simulated = 0
    while len(order) < n and any(by_cat.values()):
        live = [c for c, lst in by_cat.items() if lst]
        # "" (never posted) sorts first; real timestamps start with a digit; the
        # simulated "~" entries sort after both, i.e. "most recent".
        chosen = min(live, key=lambda c: (recency.get(c, ""), by_cat[c][0]["id"]))
        order.append(by_cat[chosen].pop(0))
        simulated += 1
        recency[chosen] = f"~{simulated:06d}"
    return order


# ------------------------------------------------------------------ the store
class Store:
    def __init__(self, path):
        self.path = path
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        self._lock = threading.RLock()
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=15)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # -------------------------------------------------------------- categories
    def categories(self, uid):
        """Category names she has used, most recently used first."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT category, MAX(id) AS m FROM queue WHERE uid=? "
                "GROUP BY lower(category) ORDER BY m DESC", (uid,)).fetchall()
        return [r["category"] for r in rows]

    def canonical_category(self, uid, name):
        """Reuse her existing spelling ('finance' -> 'Finance'); tidy brand-new names."""
        name = " ".join((name or "").split())[:40] or "General"
        for existing in self.categories(uid) + DEFAULT_CATEGORIES:
            if category_key(existing) == category_key(name):
                return existing
        return name if any(ch.isupper() for ch in name) else name.title()

    # -------------------------------------------------------------- queue items
    def add_item(self, uid, category, text, photo_file_id=None, link=None, link_title=None,
                 link_desc=None, link_image=None, scheduled_for=None):
        category = self.canonical_category(uid, category)
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO queue (uid, category, text, photo_file_id, link, link_title, "
                "link_desc, link_image, scheduled_for, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (uid, category, text, photo_file_id, link, link_title, link_desc, link_image,
                 _utc_str(scheduled_for) if scheduled_for else None, _utc_str(utcnow())))
            return cur.lastrowid

    def get_item(self, item_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM queue WHERE id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def list_queued(self, uid, pinned=False):
        """Waiting items. pinned=False -> the rotation pool; pinned=True -> one-off dated posts."""
        cond = "scheduled_for IS NOT NULL" if pinned else "scheduled_for IS NULL"
        order = "scheduled_for, id" if pinned else "id"
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM queue WHERE uid=? AND status='queued' AND {cond} ORDER BY {order}",
                (uid,)).fetchall()
        return [dict(r) for r in rows]

    def counts(self, uid):
        """{'Finance': 4, 'Lifestyle': 3} for the rotation pool."""
        out = {}
        for item in self.list_queued(uid):
            out[item["category"]] = out.get(item["category"], 0) + 1
        return out

    def last_posted(self, uid):
        with self._conn() as c:
            rows = c.execute(
                "SELECT lower(category) AS k, MAX(posted_at) AS t FROM queue "
                "WHERE uid=? AND status='posted' GROUP BY lower(category)", (uid,)).fetchall()
        return {r["k"]: r["t"] for r in rows if r["t"]}

    def upcoming(self, uid, n=5):
        return plan_order(self.list_queued(uid), self.last_posted(uid), n)

    def next_item(self, uid):
        found = self.upcoming(uid, 1)
        return found[0] if found else None

    def delete_item(self, uid, item_id):
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM queue WHERE id=? AND uid=? AND status IN ('queued','failed')",
                (item_id, uid))
            return cur.rowcount == 1

    def claim(self, item_id):
        """Atomically take an item so it can never be posted twice."""
        with self._conn() as c:
            cur = c.execute("UPDATE queue SET status='posting' WHERE id=? AND status='queued'",
                            (item_id,))
            return cur.rowcount == 1

    def mark_posted(self, item_id, url=None):
        with self._conn() as c:
            c.execute("UPDATE queue SET status='posted', posted_at=?, result_url=?, error=NULL "
                      "WHERE id=?", (_utc_str(utcnow()), url, item_id))

    def mark_failed(self, item_id, error):
        with self._conn() as c:
            c.execute("UPDATE queue SET status='failed', error=? WHERE id=?",
                      (str(error)[:500], item_id))

    def requeue(self, item_id):
        with self._conn() as c:
            cur = c.execute("UPDATE queue SET status='queued', error=NULL "
                            "WHERE id=? AND status='failed'", (item_id,))
            return cur.rowcount == 1

    def recover_interrupted(self):
        """After a crash/restart, anything stuck 'posting' may or may not have gone out.
        Don't guess and risk a double post - mark it failed so she can check and retry."""
        with self._conn() as c:
            rows = c.execute("SELECT * FROM queue WHERE status='posting'").fetchall()
            c.execute("UPDATE queue SET status='failed', error='Interrupted by a restart - "
                      "check LinkedIn before retrying' WHERE status='posting'")
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- pinned one-offs
    def due_pinned(self, now_utc, late_minutes=120):
        lo = _utc_str(now_utc - timedelta(minutes=late_minutes))
        hi = _utc_str(now_utc)
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM queue WHERE status='queued' AND scheduled_for IS NOT NULL "
                "AND scheduled_for<=? AND scheduled_for>? ORDER BY scheduled_for",
                (hi, lo)).fetchall()
        return [dict(r) for r in rows]

    def expire_pinned(self, now_utc, late_minutes=120):
        """One-offs that are far overdue (bot was down) are not posted at a random later
        hour - they are marked missed so she can decide."""
        lo = _utc_str(now_utc - timedelta(minutes=late_minutes))
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM queue WHERE status='queued' AND scheduled_for IS NOT NULL "
                "AND scheduled_for<=?", (lo,)).fetchall()
            c.execute("UPDATE queue SET status='failed', error='Missed its scheduled time' "
                      "WHERE status='queued' AND scheduled_for IS NOT NULL AND scheduled_for<=?",
                      (lo,))
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- weekly slots
    def add_slot(self, uid, dow, hhmm, now_local=None):
        """False if she already had it. A slot whose time has already passed today
        starts next week instead of firing immediately."""
        last = None
        if now_local is not None and now_local.weekday() == dow \
                and hhmm <= now_local.strftime("%H:%M"):
            last = now_local.strftime("%Y-%m-%d")
        try:
            with self._conn() as c:
                c.execute("INSERT INTO slots (uid, dow, hhmm, last_fired) VALUES (?,?,?,?)",
                          (uid, dow, hhmm, last))
            return True
        except sqlite3.IntegrityError:
            return False

    def list_slots(self, uid):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM slots WHERE uid=? ORDER BY dow, hhmm", (uid,)).fetchall()
        return [dict(r) for r in rows]

    def remove_slot_id(self, uid, slot_id):
        with self._conn() as c:
            return c.execute("DELETE FROM slots WHERE id=? AND uid=?", (slot_id, uid)).rowcount == 1

    def remove_slots(self, uid, dows=None, hhmm=None):
        """Remove by day(s), by time, or both. Needs at least one filter."""
        if not dows and not hhmm:
            return 0
        sql, args = "DELETE FROM slots WHERE uid=?", [uid]
        if dows:
            sql += f" AND dow IN ({','.join('?' * len(dows))})"
            args += list(dows)
        if hhmm:
            sql += " AND hhmm=?"
            args.append(hhmm)
        with self._conn() as c:
            return c.execute(sql, args).rowcount

    def due_slots(self, now_local, grace_min=GRACE_MINUTES):
        """Slots that should fire right now (not paused, not already fired today)."""
        today = now_local.strftime("%Y-%m-%d")
        now_min = now_local.hour * 60 + now_local.minute
        with self._conn() as c:
            rows = c.execute(
                "SELECT s.* FROM slots s LEFT JOIN prefs p ON p.uid = s.uid "
                "WHERE s.dow=? AND COALESCE(p.paused,0)=0 "
                "AND (s.last_fired IS NULL OR s.last_fired<>?)",
                (now_local.weekday(), today)).fetchall()
        due = []
        for r in rows:
            slot_min = int(r["hhmm"][:2]) * 60 + int(r["hhmm"][3:])
            if 0 <= now_min - slot_min < grace_min:
                due.append(dict(r))
        return due

    def mark_fired(self, slot_id, local_date):
        with self._conn() as c:
            c.execute("UPDATE slots SET last_fired=? WHERE id=?", (local_date, slot_id))

    def next_slot(self, uid, now_local):
        """The next upcoming slot time (aware datetime) or None."""
        slots = self.list_slots(uid)
        best = None
        for offset in range(0, 8):
            day = now_local + timedelta(days=offset)
            for s in slots:
                if s["dow"] != day.weekday():
                    continue
                dt = day.replace(hour=int(s["hhmm"][:2]), minute=int(s["hhmm"][3:]),
                                 second=0, microsecond=0)
                if dt > now_local and (best is None or dt < best):
                    best = dt
        return best

    # -------------------------------------------------------------- prefs
    def set_paused(self, uid, paused):
        with self._conn() as c:
            c.execute("INSERT INTO prefs (uid, paused) VALUES (?,?) "
                      "ON CONFLICT(uid) DO UPDATE SET paused=excluded.paused",
                      (uid, 1 if paused else 0))

    def is_paused(self, uid):
        with self._conn() as c:
            row = c.execute("SELECT paused FROM prefs WHERE uid=?", (uid,)).fetchone()
        return bool(row and row["paused"])
