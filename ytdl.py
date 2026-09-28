#!/usr/bin/env python3
"""Download YouTube videos or their audio, built on yt-dlp.

    python3 ytdl.py URL                     # MP4 video, best quality
    python3 ytdl.py URL --quality 720       # MP4 video, at most 720p
    python3 ytdl.py URL --mp3               # MP3 audio only
    python3 ytdl.py URL URL2 -o ~/Music     # several at once, into a folder
    python3 ytdl.py PLAYLIST_URL            # every video in a playlist

Only download videos you have the right to: your own uploads, Creative
Commons content, or anything the owner allows. See README.md.
"""
import argparse
import os
import re
import shutil
import sys
from urllib.parse import urlparse

QUALITIES = ["best", "2160", "1440", "1080", "720", "480", "360"]
DEFAULT_FOLDER = os.path.join(os.path.expanduser("~"), "Downloads", "YtDownloader")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
                 "youtu.be", "www.youtu.be"}


class DownloadError(Exception):
    """A download failed; the message is safe to show to a user."""


class QuietLogger:
    """Collects yt-dlp's messages instead of printing them, so errors are
    reported once, in our own words, with the reason yt-dlp gave."""

    def __init__(self):
        self.errors = []

    def debug(self, msg):
        pass

    info = warning = debug

    def error(self, msg):
        self.errors.append(msg)


def is_youtube_url(text):
    """True for youtube.com / youtu.be links (videos, shorts, playlists)."""
    try:
        url = urlparse(text.strip())
    except ValueError:
        return False
    return url.scheme in ("http", "https") and (url.hostname or "").lower() in YOUTUBE_HOSTS


def bundled(name):
    """Path to a program packed inside the .exe build (see build.py), or None."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    path = os.path.join(base, "bin", name + (".exe" if sys.platform == "win32" else ""))
    return path if os.path.isfile(path) else None


def find_ffmpeg():
    """Path to ffmpeg: the .exe's own copy, one on PATH, or imageio-ffmpeg's."""
    found = bundled("ffmpeg") or shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:          # not installed, or no binary for this platform
        return None


def find_deno():
    """Deno from the .exe, from PATH, or from the `deno` pip package."""
    found = bundled("deno") or shutil.which("deno")
    if found:
        return found
    try:
        import deno
        return deno.find_deno_bin()
    except (ImportError, FileNotFoundError):
        return None


def js_runtimes():
    """JavaScript runtimes yt-dlp can use to solve YouTube's player challenges."""
    deno_path = find_deno()
    if deno_path:
        return {"deno": {"path": deno_path}}
    found = {name: {} for name in ("node", "bun") if shutil.which(name)}
    return found or {"deno": {}}      # yt-dlp's default; it explains what's missing


def format_options(audio, quality, ffmpeg):
    """yt-dlp options that pick the formats to download.

    With ffmpeg, the best video and audio streams are downloaded separately
    and merged (that's how YouTube serves anything above 720p). Without it,
    only single-file formats work, so quality tops out lower.
    """
    if audio:
        if ffmpeg:
            return {
                "format": "bestaudio/best",
                "postprocessors": [{"key": "FFmpegExtractAudio",
                                    "preferredcodec": "mp3", "preferredquality": "192"}],
            }
        return {"format": "bestaudio[ext=m4a]/bestaudio/best"}

    limit = "" if quality == "best" else f"[height<={int(quality)}]"
    if ffmpeg:
        return {
            "format": f"bestvideo{limit}+bestaudio/best{limit}/best",
            # Highest resolution first, then prefer H.264 video and AAC audio so
            # the MP4 plays in any player (YouTube has them up to 1080p; above
            # that only VP9/AV1 exist, which modern players handle).
            "format_sort": ["res", "vcodec:h264", "acodec:m4a"],
            "merge_output_format": "mp4",
        }
    return {"format": f"best{limit}[ext=mp4]/best{limit}/best"}


def build_options(folder, audio=False, quality="best", playlist=True, progress=None, logger=None):
    """All the yt-dlp options for one download job."""
    if quality not in QUALITIES:
        raise ValueError(f"quality must be one of {', '.join(QUALITIES)}")
    ffmpeg = find_ffmpeg()
    options = {
        "outtmpl": os.path.join(folder, "%(title)s [%(id)s].%(ext)s"),
        "noplaylist": not playlist,
        "js_runtimes": js_runtimes(),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "windowsfilenames": True,          # names that are safe on every OS
        "ignoreerrors": "only_download",    # one bad playlist item doesn't stop the rest
        "progress_hooks": [progress] if progress else [],
        "postprocessor_hooks": [],
        "logger": logger or QuietLogger(),
    }
    if ffmpeg:
        options["ffmpeg_location"] = ffmpeg
    options.update(format_options(audio, quality, ffmpeg))
    return options


def progress_event(status):
    """Turn a yt-dlp progress hook dict into a small, JSON-friendly dict."""
    total = status.get("total_bytes") or status.get("total_bytes_estimate")
    done = status.get("downloaded_bytes") or 0
    info = status.get("info_dict") or {}
    return {
        "state": status.get("status"),            # downloading / finished / error
        "percent": round(100 * done / total, 1) if total else None,
        "speed": status.get("speed"),             # bytes per second
        "eta": status.get("eta"),                 # seconds
        # "Clip.f251.webm" -> "Clip.webm": drop the stream code of a part file
        "file": re.sub(r"\.f\d+(?=\.\w+$)", "", os.path.basename(status.get("filename") or "")),
        "title": info.get("title"),
        "index": info.get("playlist_index"),
        "count": info.get("n_entries") or info.get("playlist_count"),
    }


def human_size(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def lookup(url):
    """Title, channel, duration and thumbnail for a URL, without downloading."""
    import yt_dlp
    options = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": QuietLogger(),
               "extract_flat": "in_playlist", "js_runtimes": js_runtimes()}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadError(clean_error(exc)) from None
    entries = info.get("entries")
    return {
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail") or _first_thumbnail(info),
        "playlist": entries is not None,
        "count": len(list(entries)) if entries is not None else 1,
    }


def _first_thumbnail(info):
    thumbs = info.get("thumbnails") or []
    return thumbs[-1]["url"] if thumbs else None


def clean_error(exc):
    """yt-dlp errors start with 'ERROR: [youtube] id: '; keep the useful part."""
    text = str(exc).replace("ERROR: ", "")
    if "] " in text and text.startswith("["):
        text = text.split("] ", 1)[1]
    if ": " in text and len(text.split(": ", 1)[0]) == 11:     # video id prefix
        text = text.split(": ", 1)[1]
    return text.strip()


def download(urls, folder=DEFAULT_FOLDER, audio=False, quality="best", playlist=True, progress=None):
    """Download each URL into folder. Raises DownloadError if nothing worked."""
    import yt_dlp
    os.makedirs(folder, exist_ok=True)
    logger = QuietLogger()
    options = build_options(folder, audio, quality, playlist, progress, logger)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            code = ydl.download(list(urls))
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadError(clean_error(exc)) from None
    if code:
        reason = clean_error(logger.errors[-1]) if logger.errors else "The download failed."
        failed = len(logger.errors)
        if failed > 1:
            reason = f"{failed} downloads failed. Last error: {reason}"
        raise DownloadError(reason)


# ---------------------------------------------------------------- command line

def print_progress(status):
    event = progress_event(status)
    if event["state"] == "downloading":
        pct = f"{event['percent']:5.1f}%" if event["percent"] is not None else "  ?  "
        speed = human_size(event["speed"]) + "/s" if event["speed"] else ""
        eta = f"ETA {event['eta']}s" if event["eta"] is not None else ""
        bar = "#" * int((event["percent"] or 0) / 5)
        sys.stderr.write(f"\r  [{bar:<20}] {pct} {speed:>11} {eta:<9}")
        sys.stderr.flush()
    elif event["state"] == "finished":
        sys.stderr.write(f"\r  [{'#' * 20}] done: {event['file']}\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+", metavar="URL", help="YouTube video, short or playlist links")
    kind = ap.add_mutually_exclusive_group()
    kind.add_argument("--mp4", dest="audio", action="store_false", help="MP4 video (the default)")
    kind.add_argument("--mp3", "-a", "--audio", dest="audio", action="store_true", help="MP3 audio only")
    ap.set_defaults(audio=False)        # MP4 unless --mp3 is given
    ap.add_argument("-q", "--quality", choices=QUALITIES, default="best",
                    help="highest video resolution to download (default: best)")
    ap.add_argument("-o", "--output", default=DEFAULT_FOLDER,
                    help=f"folder to save into (default: {DEFAULT_FOLDER})")
    ap.add_argument("--no-playlist", action="store_true",
                    help="for a video link inside a playlist, get just that video")
    args = ap.parse_args(argv)

    bad = [u for u in args.urls if not is_youtube_url(u)]
    if bad:
        ap.error("not a YouTube link: " + ", ".join(bad))
    if not find_ffmpeg():
        print("Note: ffmpeg not found, so video is limited to single-file formats and audio "
              "stays m4a. Run: pip install -r requirements.txt", file=sys.stderr)

    folder = os.path.abspath(os.path.expanduser(args.output))
    what = "MP3 audio" if args.audio else f"MP4 video ({args.quality})"
    print(f"Downloading {what} to {folder}", file=sys.stderr)
    try:
        download(args.urls, folder, args.audio, args.quality, not args.no_playlist, print_progress)
    except DownloadError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    print("All done.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
