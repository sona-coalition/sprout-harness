#!/usr/bin/env bash
# ============================================================================
# start.sh — builds and starts Sprout in a container, behind the egress proxy.
#
# Run it from the harness root:   ./start.sh
#
# What this script does, in order:
#   1. Checks the secrets file, and clones or pulls the content repositories
#      itself — the operator never assembles them by hand.
#   2. Builds two container images: the agent and the egress proxy.
#   3. Creates two container networks: one with no route out, one with access.
#   4. Starts the egress proxy attached to both networks.
#   5. Starts the Telegram listener as a daemon container (unless
#      SPROUT_LISTENER=off), so `start.sh` is the whole story (organisation#22).
#   6. Starts the agent attached ONLY to the closed network, so its only
#      way to reach the internet is through the proxy.
#
# Expected layout (plain nested clones; each outer repo gitignores the
# nested one, so git never confuses the levels):
#
#   sprout-harness/            <- this repository (NEVER mounted into the container)
#     .env                     <- your secrets file (gitignored; see below)
#     sprout/                  <- clone of sprout-graft/sprout         -> mounted read-write
#       sprout-private/        <- clone of sprout-graft/sprout-private -> mounted read-write
#
# The rule that makes this safe: the harness (this repo, including this
# script) is never visible inside the container, and this script never
# sources or evaluates anything from the mounted directories. Content stays
# content; controls stay outside.
# ============================================================================

set -euo pipefail   # stop on any error, on unset variables, and on pipe failures
cd "$(dirname "$0")"   # work from this script's own directory, wherever it was called from

# --- Settings (override from the environment if ever needed) ---------------
MEMORY_LIMIT="${SPROUT_MEM:-6g}"     # RAM cap for the agent container
CPU_LIMIT="${SPROUT_CPUS:-3}"        # CPU cap for the agent container

# --- 1. Locate the secrets file --------------------------------------------
# Preferred: ./.env in the harness root (gitignored — never committed).
# Fallback: ~/.config/sprout/env. SPROUT_ENV overrides both.
# The file is passed to podman with --env-file, so secret VALUES never
# appear in shell history or in the process list.
if [ -n "${SPROUT_ENV:-}" ]; then
  ENV_FILE="$SPROUT_ENV"
elif [ -f ./.env ]; then
  ENV_FILE="./.env"
else
  ENV_FILE="$HOME/.config/sprout/env"
fi

fail() { echo "start.sh: $*" >&2; exit 1; }

[ -f "$ENV_FILE" ] || fail "missing secrets file: $ENV_FILE
  Create it from runbook/env.example (fill in the four values, chmod 600).
  Put it at ./.env here in the harness root, or at ~/.config/sprout/env."

# If the secrets file lives inside this repository, it must be untracked.
# (A tracked .env would mean secrets had been committed — stop immediately.)
if [ "$ENV_FILE" = "./.env" ] && git ls-files --error-unmatch .env >/dev/null 2>&1; then
  fail ".env is tracked by git — secrets must never be committed. Remove it from git history first."
fi

# --- 2. Ensure the content clones -------------------------------------------
# The agent reads its identity and memory from these two repositories.
# Missing? Clone them. Present? Pull the latest. Either way the operator
# never has to think about the nested structure.
# Note: `git -C <dir> ...` runs git as if it were started inside <dir> —
# handy for scripting the nested clones without cd-ing around.
# The decision is "pull if it is a repository, clone if it is not":
# `rev-parse` is the robust test (it works whether .git is a directory
# or a gitfile), so an existing repo always gets a pull, never a clone.
if git -C sprout rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C sprout pull -q --ff-only \
    || fail "sprout/ has local changes or has diverged — resolve by hand"
else
  # git clone refuses a non-empty directory; catch that case with a
  # clearer message first (e.g. a half-deleted earlier attempt).
  [ -z "$(ls -A sprout 2>/dev/null)" ] \
    || fail "sprout/ exists but is not a git clone — move it aside and re-run"
  echo "Cloning sprout-graft/sprout..."
  git clone -q https://github.com/sprout-graft/sprout sprout \
    || fail "could not clone sprout-graft/sprout"
fi

# The private subsona holds the LIVE lock. Running without it would risk two
# live instances (split-brain; see runbook §7), so this is mandatory — but
# the script fetches it for you. Cloning a private repo may ask for your
# GitHub credentials (the caregiver is a collaborator).
if git -C sprout/sprout-private rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C sprout/sprout-private pull -q --ff-only \
    || fail "sprout/sprout-private/ has local changes or has diverged — resolve by hand"
else
  [ -z "$(ls -A sprout/sprout-private 2>/dev/null)" ] \
    || fail "sprout/sprout-private/ exists but is not a git clone — move it aside and re-run"
  echo "Cloning sprout-graft/sprout-private..."
  git clone -q https://github.com/sprout-graft/sprout-private sprout/sprout-private \
    || fail "could not clone sprout-graft/sprout-private (needs the caregiver's GitHub credentials)"
fi

# --- 2.5 The LIVE lock, enforced -------------------------------------------
# The agent checks the lock itself (AGENTS.md), but instructions can be
# skimmed — proven 2026-11-01, when a casually greeted instance answered
# "Hi" without ever looking. So the harness checks too: a fresh LIVE lock
# from another live host stops the run here, before anything is built.
LOCK=sprout/sprout-private/LIVE
if [ -f "$LOCK" ]; then
  lock_host=$(sed -n 's/^host:[[:space:]]*//p' "$LOCK" | head -1)
  lock_started=$(sed -n 's/^started:[[:space:]]*//p' "$LOCK" | head -1)
  lock_epoch=$(sed -n 's/^started_epoch:[[:space:]]*//p' "$LOCK" | head -1)
  now_epoch=$(date -u +%s)
  if [ -z "$lock_epoch" ] && [ -n "$lock_started" ]; then
    # Older locks have only the ISO timestamp; parse it (GNU date, then BSD).
    lock_epoch=$(date -u -d "$lock_started" +%s 2>/dev/null \
      || date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$lock_started" +%s 2>/dev/null \
      || echo "")
  fi
  [ -n "$lock_epoch" ] \
    || fail "LIVE lock has no readable start time (host: ${lock_host:-unknown}). Treating as fresh; see runbook §10 for the override."
  age=$(( now_epoch - lock_epoch ))
  if [ "$age" -lt 14400 ]; then
    fail "LIVE lock is fresh: host '${lock_host:-unknown}', started ${lock_started:-unknown} (${age}s ago). Another Sprout is live — stop it first, or override per runbook §10."
  fi
  echo "start.sh: stale LIVE lock (${age}s old) — taking over."
fi

# --- 3. Build the images ----------------------------------------------------
# -q keeps the output quiet; podman caches layers, so rebuilds are fast.
echo "Building images..."
podman build -q -t sprout        -f agent/Containerfile  agent
podman build -q -t sprout-egress -f egress/Containerfile egress

# --- 4. Create the networks -------------------------------------------------
# sprout-inside is "internal": containers on it have NO route to the internet
# or to the host's local network. sprout-outside is a normal network.
# `podman network exists` makes these lines safe to run repeatedly.
podman network exists sprout-inside  >/dev/null 2>&1 || podman network create --internal sprout-inside
podman network exists sprout-outside >/dev/null 2>&1 || podman network create sprout-outside

# --- 5. (Re)start the egress proxy ------------------------------------------
# The proxy sits on BOTH networks: it can reach the internet, and the agent
# can reach it. It clones sona-coalition/sprout-harness itself and polls the
# allowlist — so merging a domain PR is the ONLY thing the caregiver does.
echo "(Re)starting egress proxy..."
podman rm -f sprout-egress >/dev/null 2>&1 || true
podman run -d --name sprout-egress \
  --network sprout-inside --network sprout-outside \
  --restart unless-stopped \
  sprout-egress

# --- 6. Start the agent ------------------------------------------------------
# Key points:
#   --network sprout-inside   the agent has no direct route out; the proxy
#                             (HTTP_PROXY/HTTPS_PROXY) is its only path.
#   --env-file                secrets arrive from the file, not the shell.
#   --memory/--cpus/--pids    a runaway agent cannot take the host down.
#   -v ... :Z                 mounts the two content clones read-write,
#                             relabelled so SELinux hosts allow access.
#                             Mount paths MIRROR the host layout — same
#                             path inside the container as outside it.
# `exec` replaces this script with the podman process, so stopping the
# container (Ctrl-C) ends the script cleanly. --rm tidies up on exit.
# --- 5.5 Start the Telegram listener (daemon container) ---------------------
# A second container from the SAME image, running the listener as a daemon:
# it polls the support entity's chat and hands messages to `pi -p` inside
# itself. Same env file, same mounts, same egress proxy as the agent.
# Set SPROUT_LISTENER=off to skip it.
#
# Concurrency note (v1): if a human is mid-conversation in the interactive
# session below (which then holds the LIVE lock), the listener PARKS — it
# rechecks the lock instead of exiting, because under --restart (#16) an
# exiting guard would crash-loop. Once polling, a listener-booted `pi -p`
# that sees a fresh lock politely reports not-live in the listener's logs
# — no corruption, no double-self; the next message retries. The
# resident-mode successor (runbook §6 note) removes this overlap properly.
if [ "${SPROUT_LISTENER:-on}" != "off" ]; then
  echo "(Re)starting Telegram listener (SPROUT_LISTENER=off to disable)..."
  podman rm -f sprout-telegram >/dev/null 2>&1 || true
  podman run -d --name sprout-telegram \
    --restart unless-stopped \
    --network sprout-inside \
    --env-file "$ENV_FILE" \
    -e HTTP_PROXY=http://sprout-egress:3128 \
    -e HTTPS_PROXY=http://sprout-egress:3128 \
    -e http_proxy=http://sprout-egress:3128 \
    -e https_proxy=http://sprout-egress:3128 \
    --memory 1g --cpus 1 --pids-limit 256 \
    -v "$PWD/sprout:/home/sprout/sprout:Z" \
    -v "$PWD/sprout/sprout-private:/home/sprout/sprout/sprout-private:Z" \
    sprout telegram-listener.py
  echo "  (listener logs: podman logs -f sprout-telegram)"
fi

# --- 6. Start the agent ------------------------------------------------------
echo "Starting Sprout (memory ${MEMORY_LIMIT}, cpus ${CPU_LIMIT})..."
exec podman run -it --rm \
  --name sprout \
  --network sprout-inside \
  --env-file "$ENV_FILE" \
  -e HTTP_PROXY=http://sprout-egress:3128 \
  -e HTTPS_PROXY=http://sprout-egress:3128 \
  -e http_proxy=http://sprout-egress:3128 \
  -e https_proxy=http://sprout-egress:3128 \
  --memory "$MEMORY_LIMIT" --cpus "$CPU_LIMIT" --pids-limit 512 \
  -v "$PWD/sprout:/home/sprout/sprout:Z" \
  -v "$PWD/sprout/sprout-private:/home/sprout/sprout/sprout-private:Z" \
  sprout
