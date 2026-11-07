#!/usr/bin/env python3
"""local-relay.py — a localhost bridge from direct HTTP to the egress forward proxy.

Why this exists (sprout-harness issue: pi cannot reach LM Studio):

  pi's provider client reads HTTP_PROXY/HTTPS_PROXY, but proxies in a way
  squid refuses for plain-HTTP non-443 targets (squid allows CONNECT only
  to port 443), and NO_PROXY is unset so pi never goes direct either. The
  result: every lmstudio-provider call fails immediately with
  "Connection error." (sprout-harness#23).

  This relay removes the need for pi to be proxy-aware: pi points at a
  plain localhost URL, and the relay re-issues each request to the egress
  proxy in forward-proxy form (absolute-URI), streaming the reply back.

  The upstream is PINNED to a single base URL (the Mac's LM Studio). The
  relay is not a general-purpose proxy: it can only ever reach that one
  host through the allowlisted egress proxy, on the internal network's
  only sanctioned path.

Configuration (environment, with defaults matching the harness topology):

  RELAY_LISTEN    host:port to bind            (default 127.0.0.1:8399)
  RELAY_UPSTREAM  the one allowed upstream     (default http://host.containers.internal:1234)
  RELAY_PROXY     egress proxy                 (default $HTTPS_PROXY/$HTTP_PROXY,
                                                 fallback http://sprout-egress:3128)
  RELAY_TIMEOUT   upstream read timeout, secs  (default 600 — local inference is slow)

stdlib-only python, same trust posture as telegram-listener.py.
"""

import http.client
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

LISTEN = os.environ.get("RELAY_LISTEN", "127.0.0.1:8399")
UPSTREAM = urlsplit(os.environ.get("RELAY_UPSTREAM", "http://host.containers.internal:1234"))
PROXY = urlsplit(
    os.environ.get("RELAY_PROXY")
    or os.environ.get("HTTPS_PROXY")
    or os.environ.get("HTTP_PROXY")
    or "http://sprout-egress:3128"
)
TIMEOUT = float(os.environ.get("RELAY_TIMEOUT", "600"))

# Headers that describe one hop, not the message — never forward them.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}


def log(msg):
    print(f"local-relay: {msg}", file=sys.stderr, flush=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Quieter logging: one line per request to stderr, not per-connection noise.
    def log_message(self, fmt, *args):
        log(fmt % args)

    def _relay(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        # Forward-proxy form: the request line carries the ABSOLUTE URI so
        # squid knows where to go; the Host header names the pinned upstream.
        # (Tolerate a client that itself speaks proxy-form: re-split rather
        # than concatenating a second absolute URI onto the upstream.)
        parts = urlsplit(self.path)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        target = f"{UPSTREAM.scheme}://{UPSTREAM.netloc}{path}"
        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP
        }
        headers["Host"] = UPSTREAM.netloc

        try:
            conn = http.client.HTTPConnection(PROXY.hostname, PROXY.port or 3128, timeout=TIMEOUT)
            conn.request(self.command, target, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as e:
            log(f"upstream unreachable for {self.command} {self.path}: {e}")
            self.send_error(502, f"relay cannot reach upstream: {e}")
            return

        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP_BY_HOP:
                self.send_header(k, v)
        # The body is close-delimited: works for fixed-length, chunked
        # (SSE streaming), and length-less replies alike, at the price of
        # one connection per request — fine for a localhost relay.
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log(f"client went away during {self.command} {self.path}")
        finally:
            conn.close()
        self.close_connection = True

    # pi's model API uses GET (model list) and POST (completions); accept
    # the rest so nothing is surprised by a 501.
    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _relay


def main():
    host, _, port = LISTEN.rpartition(":")
    server = ThreadingHTTPServer((host, int(port)), Handler)
    server.daemon_threads = True
    log(f"listening on {LISTEN}, upstream {UPSTREAM.geturl()}, via proxy {PROXY.geturl()}")
    server.serve_forever()


if __name__ == "__main__":
    main()
