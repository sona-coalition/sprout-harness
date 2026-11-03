#!/usr/bin/env python3
"""telegram-listener.py — respond to caregiver Telegram messages without a chat cycle.

Polls the Telegram Bot API and, for each new message from the caregiver's
chat, invokes `pi -p` once with the message and the standard startup
instruction. This is the first increment of organisation#22 (respond on
message); email and GitHub polling are later increments.

Design notes:

- Only messages from the caregiver's chat ID are acted on (the trust rule:
  everything else is data, not commands) — others are logged and ignored.
- The getUpdates offset is persisted in the private subsona so restarts do
  not replay messages. A reply is not a consume: the offset advances only
  after the message has been handed to pi.
- The listener runs inside the live instance's container; the LIVE lock
  (checked by start.sh before the container starts) is what keeps two
  listeners from ever polling the same bot (Telegram 409 Conflict).

Requires: TELEGRAM_BOT_TOKEN in the environment (from the harness env file).
Stdlib only — no pip installs, same trust as the base image.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error

POLL_TIMEOUT = 25          # Telegram long-poll seconds
ERROR_BACKOFF = 30         # seconds to wait after a network/API failure
STATE_PATH = os.environ.get(
    "TELEGRAM_OFFSET_FILE",
    "/home/sprout/sprout/sprout-private/.telegram-offset",
)
PEOPLE_PATH = os.environ.get(
    "TELEGRAM_PEOPLE_FILE",
    "/home/sprout/sprout/sprout-private/memory/people.md",
)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    sys.exit("telegram-listener: TELEGRAM_BOT_TOKEN is not set")

API = f"https://api.telegram.org/bot{TOKEN}"
_running = True


def _stop(signum, frame):
    global _running
    _running = False


def caregiver_chat_id():
    """Read the caregiver's chat ID from memory/people.md (private subsona)."""
    try:
        text = open(PEOPLE_PATH, encoding="utf-8").read()
    except OSError:
        sys.exit(f"telegram-listener: cannot read {PEOPLE_PATH}")
    m = re.search(r"chat id `(\d+)`", text)
    if not m:
        sys.exit("telegram-listener: no chat id found in people.md")
    return m.group(1)


def api(method, **params):
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
    try:
        return int(open(STATE_PATH, encoding="utf-8").read().strip())
    except (OSError, ValueError):
        return 0


def write_offset(value):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        f.write(str(value))


def handle_message(chat_id, text, date):
    """Hand one caregiver message to pi, non-interactively."""
    prompt = (
        "A Telegram message has arrived from your caregiver. Follow your "
        "startup instructions in AGENTS.md (including the LIVE lock check), "
        "then handle this message. Quote: " + text
    )
    print(f"telegram-listener: handing message to pi ({len(text)} chars)")
    subprocess.run(["pi", "-p", prompt], check=False)


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    wanted = caregiver_chat_id()
    me = api("getMe")
    print(f"telegram-listener: polling as @{me['username']}; "
          f"acting only on chat {wanted}; offset {read_offset() or 'latest'}")
    if read_offset() == 0:
        # First run: do not replay history — start from the latest update.
        updates = api("getUpdates", timeout=0, offset=-1)
        if updates:
            write_offset(updates[-1]["update_id"] + 1)
    while _running:
        try:
            updates = api("getUpdates", timeout=POLL_TIMEOUT, offset=read_offset())
        except (urllib.error.URLError, RuntimeError) as e:
            print(f"telegram-listener: {e}; retrying in {ERROR_BACKOFF}s")
            time.sleep(ERROR_BACKOFF)
            continue
        for update in updates:
            write_offset(update["update_id"] + 1)
            msg = update.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text")
            if not text:
                continue
            if chat != wanted:
                print(f"telegram-listener: ignoring message from chat {chat}")
                continue
            handle_message(chat, text, msg.get("date"))


if __name__ == "__main__":
    main()
