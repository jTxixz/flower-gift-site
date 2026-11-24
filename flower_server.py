#!/usr/bin/env python3
"""Serve flower.html and stream frames from flower.py. Does not modify flower.py."""

import html
import os
import select
import signal
import struct
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
FLOWER = os.path.join(HERE, "flower.py")
PAGE = os.path.join(HERE, "flower.html")
PORT = 8765

CUBE = (0, 95, 135, 175, 215, 255)
ANSI16 = (
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
)


def color256(n):
    n = max(0, min(255, n))
    if n < 16:
        r, g, b = ANSI16[n]
    elif n < 232:
        n -= 16
        r = CUBE[n // 36]
        g = CUBE[(n % 36) // 6]
        b = CUBE[n % 6]
    else:
        v = 8 + (n - 232) * 10
        r = g = b = v
    return f"#{r:02x}{g:02x}{b:02x}"


def ansi_to_html(frame):
    color = None
    out = []
    buf = []

    def flush():
        if not buf:
            return
        text = html.escape("".join(buf))
        if color is None:
            out.append(text)
        else:
            out.append(f'<span style="color:{color}">{text}</span>')
        buf.clear()

    i = 0
    n = len(frame)
    while i < n:
        ch = frame[i]
        if ch == "\x1b" and i + 1 < n and frame[i + 1] == "[":
            j = i + 2
            while j < n and not ("@" <= frame[j] <= "~"):
                j += 1
            if j >= n:
                break
            cmd = frame[j]
            params = frame[i + 2:j]
            if cmd == "m":
                flush()
                if params in ("", "0"):
                    color = None
                elif params.startswith("38;5;"):
                    try:
                        color = color256(int(params.rsplit(";", 1)[-1]))
                    except ValueError:
                        color = None
            i = j + 1
            continue
        if ch == "\r":
            i += 1
            continue
        if ch == "\n":
            flush()
            out.append("\n")
        else:
            buf.append(ch)
        i += 1
    flush()
    return "".join(out)


def frames(cols, rows):
    import fcntl
    import termios

    # flower.py renders (columns - 1) by (lines - 1)
    term_cols = max(21, cols + 1)
    term_rows = max(13, rows + 1)
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", term_rows, term_cols, 0, 0))
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env["COLUMNS"] = str(term_cols)
    env["LINES"] = str(term_rows)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, "-u", FLOWER],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=HERE,
        env=env,
        start_new_session=True,
    )
    os.close(slave)
    buf = b""
    try:
        while True:
            ready, _, _ = select.select([master], [], [], 1.0)
            if proc.poll() is not None and not ready:
                break
            if not ready:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while True:
                first = buf.find(b"\x1b[H")
                if first < 0:
                    if len(buf) > 200000:
                        buf = buf[-4000:]
                    break
                second = buf.find(b"\x1b[H", first + 3)
                if second < 0:
                    break
                raw = buf[first:second].decode("utf-8", "replace")
                buf = buf[second:]
                yield ansi_to_html(raw)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            os.close(master)
        except OSError:
            pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/flower.html"):
            body = open(PAGE, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/flower_scene.js":
            body = open(os.path.join(HERE, "flower_scene.js"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/stream":
            qs = parse_qs(parsed.query)
            try:
                cols = int(qs.get("cols", ["80"])[0])
                rows = int(qs.get("rows", ["30"])[0])
            except ValueError:
                cols, rows = 80, 30
            cols = max(20, min(cols, 220))
            rows = max(12, min(rows, 80))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                for frame in frames(cols, rows):
                    payload = "".join(f"data: {line}\n" for line in frame.split("\n"))
                    self.wfile.write((payload + "\n").encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
            return
        self.send_error(404)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"flower page: http://127.0.0.1:{PORT}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
