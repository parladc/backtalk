# backtalk: talk to your Claude Code agent out loud.
# Copyright (C) 2026 Jared Rhodenizer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A text terminal on a local web page — reachable from a phone or any
browser on the LAN, so the agent is reachable without being near the
PC's mic/speakers at all. No audio either direction: you type, you read
the reply as text, same conversation as the voice line.

One page, three routes:
  GET  /           the message log + a text box
  POST /type       one typed message -> dropped on the SAME queue the
                   PC's own typed console reads from, so it is a
                   first-class turn: console verbs ("clear the
                   session"), permission answers, all of it, free.
  GET  /transcript?since=N
                   polled by the page for every line (either side)
                   with id > N, oldest first (see signals.append_transcript)

Deliberately NO auth: anything that can reach the port can talk to
your agent. That is the tradeoff for phone-off-Wi-Fi convenience with
no separate login system to lose the key to. Bound to 0.0.0.0 on
purpose so it reaches other devices on the same network — NOT the
internet, unless your router is forwarding this port, which nothing
here does for you.
"""
import json
import queue
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backtalk import signals
from backtalk.config import CFG
from backtalk.vlog import log

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<style>
  html, body {{ height: 100%; margin: 0; background: #111; color: #eee;
    font-family: -apple-system, system-ui, sans-serif; }}
  body {{ display: flex; flex-direction: column; }}
  #log {{ flex: 1; overflow-y: auto; padding: 0.75rem; }}
  .line {{ margin-bottom: 0.6rem; max-width: 85%; padding: 0.5rem 0.75rem;
    border-radius: 0.9rem; line-height: 1.35; white-space: pre-wrap;
    word-wrap: break-word; }}
  .you {{ margin-left: auto; background: #2a5c3f; color: #eee; }}
  .them {{ margin-right: auto; background: #2a2a2a; color: #eee; }}
  #bar {{ display: flex; padding: 0.5rem; gap: 0.5rem;
    border-top: 1px solid #333; }}
  #msg {{ flex: 1; border-radius: 1.2rem; border: none; padding: 0.7rem 1rem;
    background: #2a2a2a; color: #eee; font-size: 1rem; }}
  #msg:focus {{ outline: 1px solid #4ade80; }}
  #send {{ border-radius: 1.2rem; border: none; background: #4ade80;
    color: #111; padding: 0 1.2rem; font-size: 1rem; font-weight: 600; }}
  #send:active {{ background: #3abf6a; }}
</style></head>
<body>
  <div id="log"></div>
  <div id="bar">
    <input id="msg" type="text" autocomplete="off"
           placeholder="Message {name}...">
    <button id="send">Send</button>
  </div>
<script>
  const logEl = document.getElementById('log');
  const msgEl = document.getElementById('msg');
  const sendEl = document.getElementById('send');
  let lastId = 0;
  let atBottom = true;
  logEl.addEventListener('scroll', () => {{
    atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  }});
  function addLine(who, text) {{
    const d = document.createElement('div');
    d.className = 'line ' + (who === 'you' ? 'you' : 'them');
    d.textContent = text;
    logEl.appendChild(d);
    if (atBottom) logEl.scrollTop = logEl.scrollHeight;
  }}
  function poll() {{
    fetch('/transcript?since=' + lastId).then(r => r.json()).then(msgs => {{
      for (const m of msgs) {{ addLine(m.who, m.text); lastId = m.id; }}
    }}).catch(() => {{}});
  }}
  function send() {{
    const text = msgEl.value.trim();
    if (!text) return;
    msgEl.value = '';
    fetch('/type', {{method: 'POST', body: text}});
  }}
  sendEl.addEventListener('click', send);
  msgEl.addEventListener('keydown', (e) => {{
    if (e.key === 'Enter') {{ e.preventDefault(); send(); }}
  }});
  setInterval(poll, 500);
  poll();
  msgEl.focus();
</script>
</body></html>"""


def _lan_ip() -> str:
    """Best-guess LAN address to print, without sending any traffic."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _make_handler(typed_q: "queue.Queue[str]"):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # the backtalk log is the log; don't double it to stdout

        def do_GET(self):
            if self.path == "/":
                body = _PAGE.format(name=CFG["name"]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/transcript"):
                since = 0
                if "?since=" in self.path:
                    try:
                        since = int(self.path.split("?since=", 1)[1])
                    except ValueError:
                        since = 0
                body = json.dumps(signals.get_transcript(since)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            if self.path == "/type":
                n = int(self.headers.get("Content-Length") or 0)
                text = self.rfile.read(n).decode("utf-8", "replace").strip()
                if text:
                    typed_q.put(text)
                self.send_response(204)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

    return Handler


def start(port: int, typed_q: "queue.Queue[str]") -> str | None:
    """Serve the text terminal on the LAN. Never raises — a dashboard
    that fails to bind should not take the voice line down with it; it
    just logs and the rest of backtalk keeps working."""
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(typed_q))
    except OSError as e:
        log(f"[dashboard] couldn't bind port {port}: {e}")
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://{_lan_ip()}:{port}/"
    log(f"[dashboard] text terminal live at {url}")
    return url
