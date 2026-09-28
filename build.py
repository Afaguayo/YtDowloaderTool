"""Build YTDownloader.exe (or the macOS app) with PyInstaller. Output in dist/.

    pip install -r requirements.txt pyinstaller
    python build.py

ffmpeg and Deno are packed inside the build, so it runs on a PC with nothing
else installed.
"""
import os
import sys

import deno
import imageio_ffmpeg
import PyInstaller.__main__

sep = os.pathsep  # PyInstaller's src/dest separator: ';' on Windows, ':' elsewhere
exe = ".exe" if sys.platform == "win32" else ""
ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

# ytdl.bundled() looks for bin/ffmpeg and bin/deno, so copy them in under those names.
os.makedirs("build/bin", exist_ok=True)
for source, name in ((ffmpeg, "ffmpeg" + exe), (deno.find_deno_bin(), "deno" + exe)):
    target = os.path.join("build", "bin", name)
    with open(source, "rb") as src, open(target, "wb") as dst:
        dst.write(src.read())
    os.chmod(target, 0o755)

PyInstaller.__main__.run([
    "--noconfirm",
    "--onefile",
    "--windowed",
    "--name", "YTDownloader",
    f"--add-data=web{sep}web",
    f"--add-binary=build/bin/ffmpeg{exe}{sep}bin",
    f"--add-binary=build/bin/deno{exe}{sep}bin",
    "--collect-all", "yt_dlp_ejs",      # the JavaScript that solves YouTube's challenges
    "--collect-all", "yt_dlp",           # extractors are imported lazily; include them all
    "--exclude-module", "imageio_ffmpeg",  # its ffmpeg is already in bin/
    "gui.py",
])
