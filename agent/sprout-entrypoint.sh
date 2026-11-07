#!/usr/bin/env bash
# ============================================================================
# agent/sprout-entrypoint.sh — every container from this image starts here
# (the interactive `sprout` one and the `sprout-telegram` listener alike;
# start.sh names this image, so the ENTRYPOINT wraps whatever command follows).
#
# Two jobs, then the caller's command runs:
#
#   1. Start local-relay.py in the background — the pinned LM Studio bridge
#      that lets pi reach the local model through the egress proxy (see the
#      relay's own header and sprout-harness#23). SPROUT_RELAY=off disables.
#      The relay binds loopback only; if it fails to start, the failure is
#      local to the lmstudio provider — every other model path is unaffected.
#
#   2. Export NO_PROXY for loopback, so pi connects to the relay DIRECTLY.
#      Without it pi routes even 127.0.0.1 through the egress proxy, which
#      cannot (and should not) serve it — the second half of issue #23.
#      Merges with any inherited NO_PROXY rather than replacing it.
#
# Note: `podman exec <container> bash` does NOT pass through this entrypoint
# (the relay is already running from container start; only NO_PROXY would be
# missing — export it by hand in such a shell before using the lmstudio
# provider there).
# ============================================================================
set -u

if [ "${SPROUT_RELAY:-on}" != "off" ]; then
  python3 /usr/local/bin/local-relay.py >>"${SPROUT_RELAY_LOG:-/tmp/local-relay.log}" 2>&1 &
fi

case ",${NO_PROXY:-}," in
  *,127.0.0.1,*) ;;                                                    # loopback already covered
  *) export NO_PROXY="${NO_PROXY:+$NO_PROXY,}127.0.0.1,localhost" ;;
esac
export no_proxy="${no_proxy:-$NO_PROXY}"

exec "$@"
