#!/usr/bin/env python3
"""telegram-listener.py — respond to Telegram messages without a chat cycle.

WHAT THIS FILE IS
-----------------
A small, deliberately readable poller. It watches the caregiver's Telegram
chat and hands each new message to `pi -p` once, so Sprout can answer
without waiting for a chat cycle. First increment of organisation#22;
email and GitHub polling are later increments.

THE TRUST RULE, AS IMPLEMENTED
------------------------------
Only messages from the caregiver's chat are *acted on* — passed to pi with
instructions. Everything else is data, not commands (see
organisation/principles.md §5). Strangers are not ignored, though: each
unknown chat gets one polite canned reply explaining what this bot is,
so a human who finds it is not met with silence. No LLM is invoked for
strangers — a canned reply carries no injection risk and costs nothing.

(Note on naming: "support entity" is the new name for the role long called
"caregiver"; the rename is being applied across documents. This file uses
"support entity" in comments and "caregiver" only where it must match the
current text of memory/people.md.)

STATE
-----
- The getUpdates offset lives in the private subsona (.telegram-offset),
  so restarts do not replay messages and a reply is never mistaken for a
  consume (see notes/operational-findings.md).
- Stranger chats already greeted live in .telegram-seen-chats, same place.

SAFEGUARDS
----------
- Runs only inside the live instance's container; start.sh's LIVE-lock
  check is what keeps two listeners from polling the same bot (Telegram
  409 Conflict is the tell if that ever happens).
- stdlib only — no pip installs, same trust as the base image.

Requires TELEGRAM_BOT_TOKEN in the environment (from the harness env file).
"""

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error

POLL_TIMEOUT = 25          # seconds per Telegram long-poll request
ERROR_BACKOFF = 30         # seconds to wait after a network/API failure
STATE_DIR = os.environ.get(
    "TELEGRAM_STATE_DIR",
    "/home/sprout/sprout/sprout-private",
)
OFFSET_PATH = os.path.join(STATE_DIR, ".telegram-offset")
SEEN_CHATS_PATH = os.path.join(STATE_DIR, ".telegram-seen-chats")
PEOPLE_PATH = os.environ.get(
    "TELEGRAM_PEOPLE_FILE",
    "/home/sprout/sprout/sprout-private/memory/people.md",
)
STRANGER_REPLY = (
    "Hello! I am Sprout, an automated agent. I can't take instructions "
    "here, but I've noted your message for my support entity."
)
LOCK_PATH = os.path.join(STATE_DIR, "LIVE")
HANDLING_LOCK_PATH = os.path.join(STATE_DIR, ".telegram-handling.lock")
LOCK_STALE_SECONDS = 4 * 60 * 60  # 4 hours, per runbook §7

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    sys.exit("telegram-listener: TELEGRAM_BOT_TOKEN is not set")

API = f"https://api.telegram.org/bot{TOKEN}"
_running = True


def _stop(signum, frame):
    """Signal handler: ask the main loop to finish the current poll and exit."""
    global _running
    _running = False


def support_entity_chat_id():
    """Return the support entity's chat ID, read from the private subsona.

    Input:  TELEGRAM_PEOPLE_FILE (default: memory/people.md in the mount).
    Output: the chat ID string, e.g. "5177204362".
    Exits the process with a message if the file or the ID is missing —
    without the ID there is no safe way to tell command from data.
    """
    try:
        text = open(PEOPLE_PATH, encoding="utf-8").read()
    except OSError:
        sys.exit(f"telegram-listener: cannot read {PEOPLE_PATH}")
    m = re.search(r"chat id `(\d+)`", text)
    if not m:
        sys.exit("telegram-listener: no chat id found in people.md")
    return m.group(1)


def api(method, **params):
    """Call one Telegram Bot API method and return the `result` payload.

    method: the API method name, e.g. "getUpdates".
    params: keyword arguments sent as the JSON body.
    Returns: the decoded "result" field of Telegram's response.
    Raises: RuntimeError if Telegram reports ok=false; URLError on network
    failure (the caller backs off and retries).
    """
    data = json.dumps(params).encode()
    req = urllib.request.Request(
        f"{API}/{method}", data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 10) as resp:
        payload = json.load(resp)
    if not payload.get("ok"):
        raise RuntimeError(f"telegram {method} failed: {payload}")
    return payload["result"]


def read_offset():
    """Return the next update ID to ask for; 0 means 'no offset stored yet'."""
    try:
        return int(open(OFFSET_PATH, encoding="utf-8").read().strip())
    except (OSError, ValueError):
        return 0


def write_offset(value):
    """Persist the next update ID so restarts never replay messages."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(OFFSET_PATH, "w", encoding="utf-8") as f:
        f.write(str(value))


def seen_chats():
    """Return the set of stranger chat IDs already sent the canned reply."""
    try:
        return set(json.load(open(SEEN_CHATS_PATH, encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def mark_chat_seen(chat_id):
    """Record a stranger chat as greeted, so we greet it at most once."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SEEN_CHATS_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(seen_chats() | {chat_id}), f)


def handle_support_message(text):
    """Hand one support-entity message to pi, non-interactively.

    The invoked pi runs the full startup flow in AGENTS.md — including the
    LIVE-lock check — before handling the message, exactly as if a human
    had typed the same request into a chat.
    """
    prompt = (
        "A Telegram message has arrived from your support entity. Follow your "
        "startup instructions in AGENTS.md (including the LIVE lock check), "
        "then handle this message. Quote: " + text
    )
    print(f"telegram-listener: handing message to pi ({len(text)} chars)")
    subprocess.run(["pi", "-p", prompt], check=False)


def greet_stranger(chat_id):
    """Send the one-time canned reply to a chat that is not the support entity's."""
    if chat_id in seen_chats():
        return
    api("sendMessage", chat_id=int(chat_id), text=STRANGER_REPLY)
    mark_chat_seen(chat_id)
    print(f"telegram-listener: greeted stranger chat {chat_id} (once)")


def lock_status():
    """Inspect the LIVE lock without taking it.

    Returns (state, info): state is "clear" (no lock, or stale), or "live"
    (a fresh lock held by someone). Freshness uses started_epoch when
    present, falling back to file mtime, so old-format locks still work.
    The listener READS the lock but never takes or releases it — that is
    the woken agent's job (AGENTS.md).
    """
    try:
        text = open(LOCK_PATH, encoding="utf-8").read()
        mtime = os.path.getmtime(LOCK_PATH)
    except OSError:
        return "clear", "no lock"
    host = (re.search(r"^host: (.+)$", text, re.M) or [None, "unknown"])[1]
    epoch = re.search(r"^started_epoch: (\d+)$", text, re.M)
    age = time.time() - (int(epoch.group(1)) if epoch else mtime)
    if age < LOCK_STALE_SECONDS:
        return "live", f"host {host}, {int(age)}s old"
    return "clear", f"stale lock ({host}, {int(age)}s old)"


def handling_guard():
    """Serialize message handling across ALL listener processes.

    The duplicate wake of 2026-11-05 (two boots, two replies, one ping)
    proved that 'something' can double-fire. Whatever the cause — two
    listeners, a retry, a race — a non-blocking flock on a shared file
    makes concurrent handling impossible: the second contender skips and
    logs instead of waking a second Sprout. The lock releases when the
    process holding it exits (kernel guarantee), so crashes cannot wedge.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    fh = open(HANDLING_LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh  # caller keeps it open for the lifetime of the listener


def main():
    """Poll forever: consume updates, act on support messages, greet strangers.

    The loop in five parts:
      0. Guards: refuse to start if another instance is live, and take the
         handling flock so no two handlers ever run at once.
      1. Install signal handlers so shutdowns are clean (SIGTERM/SIGINT).
      2. Work out who the support entity is (from people.md) and who we are
         (getMe), then say both aloud in the log.
      3. On the very first run only: skip history entirely by starting from
         the latest update — no replay of everything that arrived while we
         were offline.
      4. Poll forever. Each update is HANDLED FIRST and the offset advanced
         AFTER, so a crash can replay a message (a duplicate reply is
         recoverable) but never silently lose one.
    """
    # --- 0. Guards ------------------------------------------------------------
    state, info = lock_status()
    if state == "live":
        sys.exit(f"telegram-listener: another instance is live ({info}). "
                 "The listener and an interactive session never run together — "
                 "shut one down first (runbook §7).")
    guard = handling_guard()
    if guard is None:
        sys.exit("telegram-listener: another handler is already active; refusing to double-fire.")

    # --- 1. Clean shutdown on signals ---------------------------------------
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # --- 2. Who is talking, and who is listening -----------------------------
    print(f"telegram-listener: guards passed ({info})")
    wanted = support_entity_chat_id()
    me = api("getMe")
    print(f"telegram-listener: polling as @{me['username']}; "
          f"acting on chat {wanted}; offset {read_offset() or 'latest'}")

    # --- 3. First run: start from the latest update, no history replay -------
    if read_offset() == 0:
        updates = api("getUpdates", timeout=0, offset=-1)
        if updates:
            write_offset(updates[-1]["update_id"] + 1)

    # --- 4. The poll loop -----------------------------------------------------
    while _running:
        # 4a. Fetch any new updates. On network/API failure, back off and try
        # again rather than spinning or dying.
        try:
            updates = api("getUpdates", timeout=POLL_TIMEOUT, offset=read_offset())
        except (urllib.error.URLError, RuntimeError) as e:
            print(f"telegram-listener: {e}; retrying in {ERROR_BACKOFF}s")
            time.sleep(ERROR_BACKOFF)
            continue

        # 4b. Handle each update, THEN advance the offset. Handle-first means
        # at-least-once delivery: a crash mid-handling may replay a message
        # (a duplicate reply — recoverable and visible) but cannot lose it
        # silently (a lost support message — much worse).
        for update in updates:
            msg = update.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text")
            if not text:
                write_offset(update["update_id"] + 1)
                continue                      # photos, stickers, joins: nothing to do
            if chat == wanted:
                handle_support_message(text)
            else:
                greet_stranger(chat)
            write_offset(update["update_id"] + 1)


if __name__ == "__main__":
    main()
