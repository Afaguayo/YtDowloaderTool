#!/usr/bin/env python3
"""Desktop app for ytdl.

The interface is web/index.html, served by a small local server and shown in a
native window (pywebview: WebKit on macOS, Edge WebView2 on Windows). Without
pywebview it opens in your default browser instead.

    python3 gui.py              # native window (or browser if pywebview isn't installed)
    python3 gui.py --browser    # always use the browser
"""
import argparse
import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import ytdl

# PyInstaller unpacks bundled files to sys._MEIPASS.
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(BASE, "web", "index.html")

# POSTs must carry this header. Browsers won't add a custom header to a
# cross-site request without a CORS preflight, which this server never
# approves, so other websites can't start downloads through it.
TOKEN_HEADER = "X-Ytdl"


class Downloads:
    """A queue of download jobs, run one at a time on a worker thread."""

    def __init__(self, folder=ytdl.DEFAULT_FOLDER, runner=None):
        self.folder = folder
        self.jobs = {}
        self.lock = threading.Lock()
        self.ids = itertools.count(1)
        self.pending = queue.Queue()
        self.runner = runner or ytdl.download        # swapped out in tests
        threading.Thread(target=self._work, daemon=True).start()

    def add(self, url, audio, quality, playlist, title=None, thumbnail=None):
        job_id = next(self.ids)
        job = {"id": job_id, "url": url, "audio": audio, "quality": quality,
               "title": title or url, "thumbnail": thumbnail, "state": "queued",
               "percent": 0, "speed": None, "eta": None, "file": None, "error": None,
               "index": None, "count": None, "folder": self.folder}
        with self.lock:
            self.jobs[job_id] = job
        self.pending.put((job_id, playlist))
        return job_id

    def snapshot(self):
        with self.lock:
            return [dict(job) for job in sorted(self.jobs.values(), key=lambda j: -j["id"])]

    def clear_finished(self):
        with self.lock:
            for job_id in [i for i, j in self.jobs.items() if j["state"] in ("done", "error")]:
                del self.jobs[job_id]

    def _update(self, job_id, **changes):
        with self.lock:
            if job_id in self.jobs:
                self.jobs[job_id].update(changes)

    def _work(self):
        while True:
            job_id, playlist = self.pending.get()
            with self.lock:
                job = dict(self.jobs.get(job_id) or {})
            if not job:
                continue
            self._update(job_id, state="downloading")

            def hook(status, job_id=job_id):
                event = ytdl.progress_event(status)
                changes = {k: event[k] for k in ("percent", "speed", "eta", "index", "count")}
                if event["title"]:
                    changes["title"] = event["title"]
                if event["file"]:
                    changes["file"] = event["file"]
                if event["state"] == "finished":
                    changes.update(state="processing", percent=100)   # merging / converting
                elif event["state"] == "downloading":
                    changes["state"] = "downloading"
                self._update(job_id, **changes)

            try:
                self.runner([job["url"]], job["folder"], job["audio"], job["quality"], playlist, hook)
                self._update(job_id, state="done", percent=100, speed=None, eta=None)
            except ytdl.DownloadError as exc:
                self._update(job_id, state="error", error=str(exc))
            except Exception as exc:          # anything unexpected still ends the job cleanly
                self._update(job_id, state="error", error=f"Something went wrong: {exc}")


def open_folder(path):
    os.makedirs(path, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform == "win32":
        os.startfile(path)                    # noqa: S606 - opens Explorer
    else:
        subprocess.Popen(["xdg-open", path])


def make_handler(downloads, lookup=ytdl.lookup):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/":
                    with open(INDEX, "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                elif url.path == "/api/state":
                    self._json(200, {"jobs": downloads.snapshot(), "folder": downloads.folder,
                                     "ffmpeg": bool(ytdl.find_ffmpeg()),
                                     "qualities": ytdl.QUALITIES})
                elif url.path == "/api/info":
                    link = params.get("url", "")
                    if not ytdl.is_youtube_url(link):
                        self._json(400, {"error": "That doesn't look like a YouTube link."})
                    else:
                        self._json(200, lookup(link))
                else:
                    self._json(404, {"error": "Not found"})
            except ytdl.DownloadError as exc:
                self._json(422, {"error": str(exc)})
            except Exception as exc:
                self._json(500, {"error": f"Something went wrong: {exc}"})

        def do_POST(self):
            if self.headers.get(TOKEN_HEADER) != "1":
                self._json(403, {"error": "Forbidden"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": "Bad request"})
                return
            path = urlparse(self.path).path
            if path == "/api/download":
                link = str(body.get("url", "")).strip()
                quality = str(body.get("quality", "best"))
                if not ytdl.is_youtube_url(link):
                    self._json(400, {"error": "That doesn't look like a YouTube link."})
                    return
                if quality not in ytdl.QUALITIES:
                    self._json(400, {"error": "Unknown quality."})
                    return
                job_id = downloads.add(link, bool(body.get("audio")), quality,
                                       bool(body.get("playlist", True)),
                                       body.get("title"), body.get("thumbnail"))
                self._json(200, {"id": job_id})
            elif path == "/api/folder":
                folder = os.path.abspath(os.path.expanduser(str(body.get("folder", "")).strip()))
                if not body.get("folder"):
                    self._json(400, {"error": "Pick a folder."})
                    return
                downloads.folder = folder
                self._json(200, {"folder": folder})
            elif path == "/api/open-folder":
                open_folder(downloads.folder)
                self._json(200, {})
            elif path == "/api/clear":
                downloads.clear_finished()
                self._json(200, {})
            else:
                self._json(404, {"error": "Not found"})

        def _json(self, code, data):
            self._send(code, json.dumps(data).encode(), "application/json")

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):   # keep the console quiet
            pass

    return Handler


class WindowApi:
    """Functions the page can call inside the pywebview window."""

    def __init__(self):
        self.window = None

    def choose_folder(self):
        import webview
        dialog = getattr(webview, "FileDialog", None)
        kind = dialog.FOLDER if dialog else webview.FOLDER_DIALOG
        result = self.window.create_file_dialog(kind)
        return result[0] if result else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--browser", action="store_true", help="open in the default browser instead of a window")
    ap.add_argument("--port", type=int, default=0, help="port for the local server (default: any free port)")
    ap.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)  # server only, for testing
    args = ap.parse_args()

    downloads = Downloads()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(downloads))  # localhost only
    url = f"http://127.0.0.1:{server.server_address[1]}/"

    if args.serve:
        print(url, flush=True)
        server.serve_forever()
        return

    threading.Thread(target=server.serve_forever, daemon=True).start()
    if not args.browser:
        try:
            import webview
        except ImportError:
            print("pywebview isn't installed; opening in your browser instead.", file=sys.stderr)
        else:
            api = WindowApi()
            api.window = webview.create_window("YT Downloader", url, js_api=api,
                                               width=880, height=760, min_size=(420, 560))
            webview.start()
            return

    webbrowser.open(url)
    print(f"YT Downloader is running at {url} (press Ctrl+C to quit).")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
