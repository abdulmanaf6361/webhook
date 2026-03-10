"""
Mock Receiver Service
---------------------
A simple HTTP server that acts as a customer's webhook endpoint.
Accepts POST requests, logs them, and returns 200 OK.
Also exposes GET /logs to see received webhooks.
"""
import json
import os
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from collections import deque

PORT = int(os.environ.get('PORT', 9000))
MAX_LOGS = 500

# Thread-safe log store
_lock = threading.Lock()
_received = deque(maxlen=MAX_LOGS)


class MockReceiverHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Override to use our own logging
        print(f"[{datetime.utcnow().isoformat()}] {format % args}")

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body_bytes = self.rfile.read(content_length) if content_length else b''

        try:
            body = json.loads(body_bytes.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {'raw': body_bytes.decode('utf-8', errors='replace')}

        entry = {
            'received_at': datetime.utcnow().isoformat(),
            'path': self.path,
            'headers': dict(self.headers),
            'body': body,
        }

        with _lock:
            _received.appendleft(entry)

        event_type = body.get('event_type', 'unknown')
        user_id = body.get('user_id', 'unknown')
        event_id = body.get('event_id', 'unknown')
        print(
            f"  ✓ RECEIVED  event_type={event_type}  user={user_id}  event_id={event_id}"
        )

        self._send_json({'status': 'ok', 'message': 'Received'}, 200)

    def do_GET(self):
        if self.path == '/logs' or self.path == '/logs/':
            with _lock:
                logs = list(_received)
            self._send_json({'count': len(logs), 'logs': logs}, 200)
        elif self.path == '/health':
            self._send_json({'status': 'healthy', 'received_count': len(_received)}, 200)
        elif self.path == '/' or self.path == '':
            html = self._build_html()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(html.encode('utf-8'))
        else:
            self._send_json({'error': 'Not found'}, 404)

    def _send_json(self, data, status):
        body = json.dumps(data, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _build_html(self):
        with _lock:
            logs = list(_received)

        rows = ''
        for entry in logs:
            body = entry['body']
            event_type = body.get('event_type', '—')
            user_id = body.get('user_id', '—')
            event_id = body.get('event_id', '—')
            rows += f"""
            <tr>
              <td>{entry['received_at']}</td>
              <td><code>{event_type}</code></td>
              <td>{user_id}</td>
              <td><code style="font-size:0.75rem">{event_id[:16]}…</code></td>
            </tr>"""

        return f"""<!DOCTYPE html>
<html>
<head>
  <title>Mock Receiver</title>
  <meta http-equiv="refresh" content="3">
  <style>
    body {{ font-family: monospace; background: #0f1117; color: #e2e8f0; padding: 2rem; }}
    h1 {{ color: #6366f1; }} h2 {{ color: #94a3b8; font-size:1rem; margin-top:1rem; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
    th {{ text-align:left; padding: 8px 12px; color: #94a3b8; border-bottom: 1px solid #2e3250; font-size:0.8rem; }}
    td {{ padding: 10px 12px; border-bottom: 1px solid #1a1d27; font-size: 0.85rem; }}
    tr:hover td {{ background: #1a1d27; }}
    code {{ color: #818cf8; }}
    .count {{ color: #22c55e; font-size: 1.5rem; font-weight: 700; }}
  </style>
</head>
<body>
  <h1>⚡ Mock Receiver</h1>
  <h2>Auto-refreshes every 3s</h2>
  <p>Total received: <span class="count">{len(logs)}</span></p>
  <table>
    <thead>
      <tr><th>Received At</th><th>Event Type</th><th>User ID</th><th>Event ID</th></tr>
    </thead>
    <tbody>{rows if rows else '<tr><td colspan=4 style="color:#94a3b8;text-align:center;padding:2rem">Waiting for webhooks…</td></tr>'}</tbody>
  </table>
  <br/><p style="color:#94a3b8;font-size:0.8rem">GET /logs for JSON • GET /health for health check</p>
</body>
</html>"""


def run():
    server = HTTPServer(('0.0.0.0', PORT), MockReceiverHandler)
    print(f"🎯 Mock Receiver listening on http://0.0.0.0:{PORT}")
    print(f"   POST /         → accept webhook deliveries")
    print(f"   GET  /logs     → JSON log of received webhooks")
    print(f"   GET  /health   → health check")
    print(f"   GET  /         → HTML dashboard (auto-refresh)")
    server.serve_forever()


if __name__ == '__main__':
    run()
