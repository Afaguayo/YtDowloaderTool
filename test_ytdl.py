"""Tests for ytdl and the app server.

    python3 -m unittest -v              # offline tests (fake downloader, no network)
    LIVE=1 python3 -m unittest -v       # also download a real 19-second video
"""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import gui
import ytdl

LIVE = bool(os.environ.get("LIVE"))
ZOO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"   # "Me at the zoo", first video on YouTube


class UrlTests(unittest.TestCase):
    def test_accepts_youtube_links(self):
        for url in [ZOO, "https://youtu.be/jNQXAC9IVRw", "https://m.youtube.com/watch?v=x",
                    "https://www.youtube.com/shorts/abc", "https://music.youtube.com/watch?v=x",
                    "https://www.youtube.com/playlist?list=PL123", "  https://youtube.com/watch?v=x  "]:
            with self.subTest(url=url):
                self.assertTrue(ytdl.is_youtube_url(url))

    def test_rejects_other_links(self):
        for url in ["", "youtube.com/watch?v=x", "ftp://youtube.com/x", "https://vimeo.com/1",
                    "https://youtube.com.evil.example/watch", "https://notyoutube.com/", "file:///etc/passwd"]:
            with self.subTest(url=url):
                self.assertFalse(ytdl.is_youtube_url(url))


class OptionTests(unittest.TestCase):
    def test_video_with_ffmpeg_merges_best_streams(self):
        opts = ytdl.format_options(False, "720", "/usr/bin/ffmpeg")
        self.assertEqual(opts["format"], "bestvideo[height<=720]+bestaudio/best[height<=720]/best")
        self.assertEqual(opts["merge_output_format"], "mp4")
        self.assertEqual(opts["format_sort"], ["res", "vcodec:h264", "acodec:m4a"])

    def test_video_best_has_no_height_limit(self):
        self.assertNotIn("height", ytdl.format_options(False, "best", "ffmpeg")["format"])

    def test_video_without_ffmpeg_uses_single_file(self):
        opts = ytdl.format_options(False, "1080", None)
        self.assertEqual(opts["format"], "best[height<=1080][ext=mp4]/best[height<=1080]/best")
        self.assertNotIn("merge_output_format", opts)

    def test_audio_converts_to_mp3_with_ffmpeg(self):
        opts = ytdl.format_options(True, "best", "ffmpeg")
        self.assertEqual(opts["postprocessors"][0]["preferredcodec"], "mp3")

    def test_audio_without_ffmpeg_keeps_m4a(self):
        opts = ytdl.format_options(True, "best", None)
        self.assertNotIn("postprocessors", opts)
        self.assertIn("m4a", opts["format"])

    def test_build_options(self):
        with mock.patch.object(ytdl, "find_ffmpeg", return_value="/x/ffmpeg"):
            opts = ytdl.build_options("/tmp/out", playlist=False)
        self.assertTrue(opts["outtmpl"].startswith("/tmp/out"))
        self.assertTrue(opts["noplaylist"])
        self.assertEqual(opts["ffmpeg_location"], "/x/ffmpeg")
        self.assertTrue(opts["js_runtimes"])

    def test_bad_quality_rejected(self):
        with self.assertRaises(ValueError):
            ytdl.build_options("/tmp", quality="999")

    def test_js_runtime_detection(self):
        with mock.patch.object(ytdl, "find_deno", return_value=None), \
                mock.patch("shutil.which", side_effect=lambda name: "/bin/node" if name == "node" else None):
            self.assertEqual(ytdl.js_runtimes(), {"node": {}})
        with mock.patch.object(ytdl, "find_deno", return_value=None), \
                mock.patch("shutil.which", return_value=None):
            self.assertEqual(ytdl.js_runtimes(), {"deno": {}})


class HelperTests(unittest.TestCase):
    def test_progress_event(self):
        event = ytdl.progress_event({
            "status": "downloading", "downloaded_bytes": 250, "total_bytes": 1000,
            "speed": 5000.0, "eta": 3, "filename": "/tmp/x/Clip [abc].webm",
            "info_dict": {"title": "Clip", "playlist_index": 2, "n_entries": 5},
        })
        self.assertEqual(event, {"state": "downloading", "percent": 25.0, "speed": 5000.0, "eta": 3,
                                 "file": "Clip [abc].webm", "title": "Clip", "index": 2, "count": 5})

    def test_progress_event_without_size(self):
        event = ytdl.progress_event({"status": "downloading", "downloaded_bytes": 10})
        self.assertIsNone(event["percent"])

    def test_clean_error(self):
        self.assertEqual(ytdl.clean_error("ERROR: [youtube] jNQXAC9IVRw: Video unavailable"),
                         "Video unavailable")
        self.assertEqual(ytdl.clean_error("ERROR: Unable to download"), "Unable to download")
        bot = ("ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you’re not a bot. Use --cookies-from-browser "
               "or --cookies for the authentication.")
        self.assertIn("VPN", ytdl.clean_error(bot))
        self.assertEqual(ytdl.clean_error("ERROR: [youtube] x: Private video. Sign in"), "This video is private.")

    def test_quiet_logger_keeps_errors(self):
        logger = ytdl.QuietLogger()
        logger.info("[youtube] Extracting URL")
        logger.warning("something")
        logger.error("ERROR: [youtube] abc: Private video")
        self.assertEqual(logger.errors, ["ERROR: [youtube] abc: Private video"])

    def test_human_size(self):
        self.assertEqual(ytdl.human_size(512), "512 B")
        self.assertEqual(ytdl.human_size(1536), "1.5 KB")
        self.assertEqual(ytdl.human_size(5 * 1024 ** 3), "5.0 GB")

    def test_cli_rejects_non_youtube_links(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as caught:
            ytdl.main(["https://example.com/video"])
        self.assertEqual(caught.exception.code, 2)

    def test_cli_passes_options_through(self):
        calls = []
        with mock.patch.object(ytdl, "download", side_effect=lambda *a: calls.append(a)), \
                mock.patch("sys.stderr"):
            code = ytdl.main([ZOO, "--mp3", "-o", "/tmp/music", "--no-playlist"])
        self.assertEqual(code, 0)
        urls, folder, audio, quality, playlist, _ = calls[0]
        self.assertEqual((urls, folder, audio, quality, playlist),
                         ([ZOO], os.path.abspath("/tmp/music"), True, "best", False))

    def test_cli_mp3_and_mp4_flags(self):
        seen = []
        with mock.patch.object(ytdl, "download", side_effect=lambda *a: seen.append(a[2])), \
                mock.patch("sys.stderr"):
            ytdl.main([ZOO])
            ytdl.main([ZOO, "--mp4"])
            ytdl.main([ZOO, "--mp3"])
            with self.assertRaises(SystemExit):
                ytdl.main([ZOO, "--mp3", "--mp4"])
        self.assertEqual(seen, [False, False, True])

    def test_js_runtime_prefers_deno_with_path(self):
        with mock.patch.object(ytdl, "find_deno", return_value="/opt/deno"):
            self.assertEqual(ytdl.js_runtimes(), {"deno": {"path": "/opt/deno"}})

    def test_bundled_programs_in_exe(self):
        with tempfile.TemporaryDirectory() as base:
            os.makedirs(os.path.join(base, "bin"))
            name = "ffmpeg.exe" if ytdl.sys.platform == "win32" else "ffmpeg"
            open(os.path.join(base, "bin", name), "w").close()
            with mock.patch.object(ytdl.sys, "_MEIPASS", base, create=True):
                self.assertEqual(ytdl.bundled("ffmpeg"), os.path.join(base, "bin", name))
                self.assertEqual(ytdl.find_ffmpeg(), os.path.join(base, "bin", name))
                self.assertIsNone(ytdl.bundled("deno"))
        self.assertIsNone(ytdl.bundled("ffmpeg"))       # not running as an .exe

    def test_cli_reports_errors(self):
        with mock.patch.object(ytdl, "download", side_effect=ytdl.DownloadError("Video unavailable")), \
                mock.patch("sys.stderr"):
            self.assertEqual(ytdl.main([ZOO]), 1)


def fake_runner(urls, folder, audio, quality, playlist, hook):
    """Stands in for ytdl.download: reports progress, then 'finishes'."""
    if "bad" in urls[0]:
        raise ytdl.DownloadError("Video unavailable")
    for done in (0, 500, 1000):
        hook({"status": "downloading", "downloaded_bytes": done, "total_bytes": 1000,
              "info_dict": {"title": "Fake video"}, "filename": os.path.join(folder, "Fake.mp4")})
    hook({"status": "finished", "downloaded_bytes": 1000, "total_bytes": 1000,
          "filename": os.path.join(folder, "Fake.mp4"), "info_dict": {"title": "Fake video"}})


def wait_until(check, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


class DownloadsTests(unittest.TestCase):
    def test_job_runs_and_finishes(self):
        downloads = gui.Downloads("/tmp/ytdl-test", runner=fake_runner)
        job_id = downloads.add(ZOO, False, "720", True)
        self.assertTrue(wait_until(lambda: downloads.snapshot()[0]["state"] == "done"))
        job = downloads.snapshot()[0]
        self.assertEqual((job["id"], job["title"], job["file"], job["percent"]),
                         (job_id, "Fake video", "Fake.mp4", 100))

    def test_failed_job_keeps_error(self):
        downloads = gui.Downloads("/tmp/ytdl-test", runner=fake_runner)
        downloads.add("https://youtu.be/bad", True, "best", True)
        self.assertTrue(wait_until(lambda: downloads.snapshot()[0]["state"] == "error"))
        self.assertEqual(downloads.snapshot()[0]["error"], "Video unavailable")

    def test_clear_finished_keeps_active_jobs(self):
        gate = threading.Event()

        def slow_runner(*args):
            gate.wait(5)

        downloads = gui.Downloads("/tmp/ytdl-test", runner=slow_runner)
        downloads.add(ZOO, False, "best", True)
        downloads.add(ZOO, False, "best", True)
        self.assertTrue(wait_until(lambda: downloads.snapshot()[-1]["state"] == "downloading"))
        downloads.clear_finished()
        self.assertEqual(len(downloads.snapshot()), 2)
        gate.set()
        self.assertTrue(wait_until(lambda: all(j["state"] == "done" for j in downloads.snapshot())))
        downloads.clear_finished()
        self.assertEqual(downloads.snapshot(), [])


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.downloads = gui.Downloads(tempfile.gettempdir(), runner=fake_runner)

        def lookup(url):
            if "missing" in url:
                raise ytdl.DownloadError("Video unavailable")
            return {"title": "Me at the zoo", "channel": "jawed", "duration": 19,
                    "thumbnail": None, "playlist": False, "count": 1}

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), gui.make_handler(self.downloads, lookup))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as res:
                return res.status, res.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()

    def post(self, path, body, token=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers[gui.TOKEN_HEADER] = "1"
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(), headers)
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_index_page(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"YT Downloader", body)

    def test_state(self):
        status, body = self.get("/api/state")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["jobs"], [])
        self.assertEqual(data["qualities"], ytdl.QUALITIES)

    def test_info(self):
        status, body = self.get("/api/info?url=" + urllib.request.quote(ZOO))
        self.assertEqual((status, json.loads(body)["title"]), (200, "Me at the zoo"))
        status, body = self.get("/api/info?url=https://example.com")
        self.assertEqual(status, 400)
        status, body = self.get("/api/info?url=" + urllib.request.quote("https://youtu.be/missing"))
        self.assertEqual((status, json.loads(body)["error"]), (422, "Video unavailable"))

    def test_download_flow(self):
        status, data = self.post("/api/download", {"url": ZOO, "audio": True, "quality": "best"})
        self.assertEqual(status, 200)
        self.assertTrue(wait_until(lambda: json.loads(self.get("/api/state")[1])["jobs"][0]["state"] == "done"))

    def test_download_validation(self):
        self.assertEqual(self.post("/api/download", {"url": "https://example.com"})[0], 400)
        self.assertEqual(self.post("/api/download", {"url": ZOO, "quality": "8k"})[0], 400)

    def test_posts_need_the_token_header(self):
        status, _ = self.post("/api/download", {"url": ZOO}, token=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.downloads.snapshot(), [])

    def test_change_folder(self):
        status, data = self.post("/api/folder", {"folder": "~/Music"})
        self.assertEqual(status, 200)
        self.assertEqual(data["folder"], os.path.abspath(os.path.expanduser("~/Music")))
        self.assertEqual(self.post("/api/folder", {"folder": ""})[0], 400)


@unittest.skipUnless(LIVE, "set LIVE=1 to download from YouTube")
class LiveTests(unittest.TestCase):
    def test_lookup(self):
        info = ytdl.lookup(ZOO)
        self.assertEqual(info["title"], "Me at the zoo")
        self.assertEqual(info["duration"], 19)

    def test_download_audio_and_video(self):
        with tempfile.TemporaryDirectory() as folder:
            ytdl.download([ZOO], folder, audio=True)
            ytdl.download([ZOO], folder, quality="360")
            names = sorted(os.listdir(folder))
            self.assertEqual(names, ["Me at the zoo [jNQXAC9IVRw].mp3", "Me at the zoo [jNQXAC9IVRw].mp4"])

    def test_missing_video_error(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(ytdl.DownloadError) as caught:
            ytdl.download(["https://www.youtube.com/watch?v=aaaaaaaaaaa"], folder)
        self.assertIn("unavailable", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
