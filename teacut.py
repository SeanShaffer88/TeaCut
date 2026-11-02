#!/usr/bin/env python3
"""
TeaCut - repair, cut and join (almost) any video file.

A modern take on the old Steeper / ASFCut tool. The original only handled
ASF and WMV; this version uses FFmpeg underneath, so it reads anything FFmpeg
can read: MP4, MKV, AVI, MOV, WMV, ASF, FLV, WebM, MPEG, TS/M2TS/MTS, VOB,
3GP, OGV, RM/RMVB, DV, MXF, and many more.

Everything is lossless and near-instant whenever possible (stream copy).
Only when that is impossible (frame-exact cuts, joining mismatched files,
badly broken files, incompatible target formats) does it re-encode.

Run with no arguments for the window, or see `teacut.py --help` for the
command-line mode.
"""

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

APP_NAME = "TeaCut"
VERSION = "1.3.0"

# Extensions offered in the file picker. FFmpeg reads far more than this;
# "All files" is always available too.
VIDEO_EXTENSIONS = (
    "mp4 m4v mkv mka webm avi divx xvid mov qt wmv asf wtv dvr-ms flv f4v "
    "mpg mpeg mpe m1v m2v mp2v mpv vob ts m2ts mts m2t tp trp 3gp 3g2 ogv ogm "
    "ogg rm rmvb dv dif mxf nut nsv y4m yuv amv mjpeg mjpg h264 264 h265 hevc "
    "265 ivf gxf roq smk bik vro evo"
).split()

# Output formats: extension -> (label, [video encoders], [audio encoders]).
# The first encoder that this FFmpeg build has is used.
FORMATS = {
    "mp4":  ("MP4 - plays everywhere", ["libx264", "mpeg4"], ["aac"]),
    "mkv":  ("MKV - holds anything", ["libx264", "mpeg4"], ["aac"]),
    "webm": ("WebM - for the web", ["libvpx-vp9", "libvpx"], ["libopus", "libvorbis"]),
    "mov":  ("MOV - Apple / editing", ["libx264", "mpeg4"], ["aac"]),
    "avi":  ("AVI - old players / DVD players", ["mpeg4"], ["libmp3lame", "mp2"]),
    "wmv":  ("WMV - Windows Media", ["wmv2", "msmpeg4"], ["wmav2"]),
    "asf":  ("ASF - Windows Media", ["wmv2", "msmpeg4"], ["wmav2"]),
    "mpg":  ("MPEG-2 - DVD style", ["mpeg2video"], ["mp2"]),
    "ts":   ("TS - broadcast / camcorder", ["libx264", "mpeg2video"], ["aac", "mp2"]),
    "flv":  ("FLV - Flash video", ["libx264", "flv"], ["aac", "libmp3lame"]),
    "m4v":  ("M4V - iTunes / Apple", ["libx264", "mpeg4"], ["aac"]),
    "3gp":  ("3GP - old phones", ["libx264", "h263"], ["aac"]),
    "ogv":  ("OGV - Ogg Theora", ["libtheora"], ["libvorbis", "flac"]),
}

VIDEO_ENCODER_ARGS = {
    "libx264": ["-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"],
    "mpeg4": ["-q:v", "3", "-pix_fmt", "yuv420p"],
    "libvpx-vp9": ["-crf", "32", "-b:v", "0", "-row-mt", "1", "-deadline", "good", "-cpu-used", "4"],
    "libvpx": ["-crf", "10", "-b:v", "4M"],
    "wmv2": ["-b:v", "5M"],
    "msmpeg4": ["-b:v", "5M"],
    "mpeg2video": ["-q:v", "3", "-pix_fmt", "yuv420p"],
    "flv": ["-q:v", "3"],
    "h263": ["-q:v", "3"],
    "libtheora": ["-q:v", "7"],
}
AUDIO_ENCODER_ARGS = {
    "aac": ["-b:a", "192k"],
    "libopus": ["-b:a", "128k"],
    "libvorbis": ["-q:a", "5"],
    "libmp3lame": ["-b:a", "192k"],
    "mp2": ["-b:a", "224k"],
    "wmav2": ["-b:a", "192k"],
    "flac": [],
}
# Convert presets. CRF is used where available, otherwise a quality value or
# a bitrate. "balanced" matches the defaults above.
QUALITY_CRF = {"smaller": 28, "balanced": 23, "high": 18}
QUALITY_Q = {"smaller": 5, "balanced": 3, "high": 2}
QUALITY_BITRATE = {"smaller": "2M", "balanced": "5M", "high": "8M"}
# Extra per-container flags.
CONTAINER_ARGS = {
    "mp4": ["-movflags", "+faststart"],
    "m4v": ["-movflags", "+faststart"],
    "mov": ["-movflags", "+faststart"],
    "avi": ["-vtag", "XVID"],
}
# Codecs each format is widely playable with: (video, audio). Streams that
# already fit are copied untouched; anything else gets re-encoded.
# None = the container takes anything.
NATIVE_CODECS = {
    "mp4": ({"h264", "hevc", "av1", "mpeg4"}, {"aac", "mp3", "ac3", "eac3", "alac"}),
    "m4v": ({"h264", "hevc", "mpeg4"}, {"aac", "ac3", "alac"}),
    "mov": ({"h264", "hevc", "mpeg4", "prores", "mjpeg"}, {"aac", "mp3", "alac", "pcm_s16le", "pcm_s24le"}),
    "3gp": ({"h264", "mpeg4", "h263"}, {"aac", "amr_nb", "amr_wb"}),
    "mkv": (None, None),
    "webm": ({"vp8", "vp9", "av1"}, {"opus", "vorbis"}),
    "avi": ({"mpeg4", "msmpeg4v2", "msmpeg4v3", "mjpeg", "h264", "dvvideo", "huffyuv"},
            {"mp3", "mp2", "ac3", "pcm_s16le"}),
    "wmv": ({"wmv1", "wmv2", "wmv3", "vc1", "msmpeg4v3"}, {"wmav1", "wmav2", "wmapro", "wmavoice"}),
    "asf": ({"wmv1", "wmv2", "wmv3", "vc1", "msmpeg4v3"}, {"wmav1", "wmav2", "wmapro", "wmavoice"}),
    "mpg": ({"mpeg1video", "mpeg2video"}, {"mp2", "mp3", "ac3"}),
    "ts": ({"h264", "hevc", "mpeg2video", "mpeg1video"}, {"aac", "mp2", "mp3", "ac3", "eac3"}),
    "flv": ({"h264", "flv1"}, {"aac", "mp3"}),
    "ogv": ({"theora", "vp8"}, {"vorbis", "opus", "flac"}),
}
# Containers that can carry subtitle streams when stream-copying.
SUBTITLE_CONTAINERS = {"mkv"}

# Input options that make FFmpeg push through damaged data.
TOLERANT_INPUT = ["-err_detect", "ignore_err", "-fflags", "+genpts+discardcorrupt+igndts"]


class TeaCutError(Exception):
    pass


class Cancelled(TeaCutError):
    pass


# ---------------------------------------------------------------------------
# FFmpeg discovery and probing
# ---------------------------------------------------------------------------

def _app_dir():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def find_ffmpeg():
    """FFmpeg next to the app wins, then PATH, then the imageio-ffmpeg package."""
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for folder in (_app_dir(), os.path.join(_app_dir(), "bin"), os.path.dirname(sys.executable)):
        candidate = os.path.join(folder, exe)
        if os.path.isfile(candidate):
            return candidate
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_quiet(args):
    return subprocess.run(args, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                          text=True, encoding="utf-8", errors="replace")


def extract_frame(ffmpeg, src, dst, at_seconds=1.0, width=160):
    """Save one small PNG frame. Returns dst, or None if there is no video."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error",
           "-ss", f"{max(0.0, at_seconds):.3f}", "-i", src,
           "-frames:v", "1", "-vf", f"scale={width}:-2", dst, "-y"]
    try:
        result = subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                creationflags=_NO_WINDOW, text=True,
                                encoding="utf-8", errors="replace")
    except OSError:
        return None
    if result.returncode != 0 or not os.path.isfile(dst) or os.path.getsize(dst) < 100:
        return None
    return dst


class MediaInfo:
    def __init__(self, path):
        self.path = path
        self.duration = 0.0
        self.has_video = False
        self.has_audio = False
        self.has_subtitles = False
        self.vcodec = self.acodec = self.scodec = None
        self.vcodecs = []
        self.acodecs = []
        self.scodecs = []
        self.width = self.height = 0
        self.fps = 0.0
        self.sample_rate = 0
        self.channels = ""
        self.container = ""

    def signature(self):
        """Files with equal signatures can be joined without re-encoding."""
        return (self.vcodec, self.width, self.height, self.acodec, self.sample_rate, self.channels)

    def summary(self):
        parts = []
        if self.duration:
            parts.append(format_time(self.duration))
        if self.has_video:
            parts.append(f"{self.width}x{self.height} {self.vcodec}")
            if self.fps:
                parts.append(f"{self.fps:g} fps")
        if self.has_audio:
            parts.append(f"{self.acodec} audio")
        if self.has_subtitles:
            parts.append(f"{self.scodec} subtitles")
        return "  ·  ".join(parts) if parts else "unknown contents"


class Engine:
    def __init__(self, ffmpeg=None):
        self.ffmpeg = ffmpeg or find_ffmpeg()
        if not self.ffmpeg:
            raise TeaCutError(
                "FFmpeg was not found.\n\n"
                "Easiest fix: run  pip install imageio-ffmpeg\n"
                "or put ffmpeg(.exe) in the same folder as TeaCut.")
        self._encoders = None

    # -- capabilities -------------------------------------------------------
    def encoders(self):
        if self._encoders is None:
            out = _run_quiet([self.ffmpeg, "-hide_banner", "-encoders"]).stdout
            self._encoders = set(re.findall(r"^\s*[VAS][\w.]{5}\s+(\S+)", out, re.M))
        return self._encoders

    def pick_encoder(self, choices):
        have = self.encoders()
        for name in choices:
            if name in have:
                return name
        return None

    # -- probing ------------------------------------------------------------
    def probe(self, path):
        if not os.path.isfile(path):
            raise TeaCutError(f"File not found: {path}")
        result = _run_quiet([self.ffmpeg, "-hide_banner", "-i", path])
        if result.returncode < 0 or result.returncode > 1:
            # Negative = killed by a signal (e.g. a segfault in the demuxer).
            raise TeaCutError(
                f"FFmpeg crashed while reading {os.path.basename(path)}. "
                "This FFmpeg build may not support that file's format.")
        text = result.stderr
        info = MediaInfo(path)
        m = re.search(r"Input #0, ([^,]+)", text)
        if m:
            info.container = m.group(1)
        m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if m:
            info.duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        for line in text.splitlines():
            m = re.search(r"Stream #\d+:\d+.*?: (Video|Audio|Subtitle): (\w+)", line)
            if not m:
                continue
            kind, codec = m.groups()
            if kind == "Video" and "attached pic" not in line:
                if codec not in info.vcodecs:
                    info.vcodecs.append(codec)
                if not info.has_video:
                    info.has_video, info.vcodec = True, codec
                    size = re.search(r", (\d{2,5})x(\d{2,5})", line)
                    if size:
                        info.width, info.height = int(size.group(1)), int(size.group(2))
                    fps = re.search(r"([\d.]+) (?:fps|tbr)", line)
                    if fps:
                        info.fps = float(fps.group(1))
            elif kind == "Audio":
                if codec not in info.acodecs:
                    info.acodecs.append(codec)
                if not info.has_audio:
                    info.has_audio, info.acodec = True, codec
                    rate = re.search(r"(\d+) Hz, ([^,]+)", line)
                    if rate:
                        info.sample_rate, info.channels = int(rate.group(1)), rate.group(2).strip()
            elif kind == "Subtitle":
                if codec not in info.scodecs:
                    info.scodecs.append(codec)
                if not info.has_subtitles:
                    info.has_subtitles, info.scodec = True, codec
        if not (info.has_video or info.has_audio):
            if "Invalid data" in text or "moov atom not found" in text:
                raise TeaCutError(
                    f"{os.path.basename(path)} is too damaged to read at all.\n"
                    "(If it is an MP4/MOV from a camera that lost power, the index is "
                    "missing entirely; a tool like 'untrunc' with a good reference file "
                    "is needed.)")
            raise TeaCutError(f"{os.path.basename(path)} does not look like a video or audio file.")
        return info

    # -- running ------------------------------------------------------------
    def run(self, args, job, total_seconds=0.0, span=(0.0, 1.0)):
        """Run ffmpeg with progress reporting. Raises TeaCutError on failure."""
        # Every call ends with the output path; make its folder first so a
        # chosen destination doesn't fail with a cryptic FFmpeg error.
        out = args[-1] if args else ""
        if out and out != "-" and not out.startswith("-"):
            try:
                ensure_parent(out)
            except OSError as e:
                raise TeaCutError(f"Cannot create the output folder for {out}: {e}")
        cmd = [self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"] + args
        job.log(" ".join(_quote(a) for a in cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
                                text=True, encoding="utf-8", errors="replace")
        job.proc = proc
        err_lines = []

        def drain():
            for line in proc.stderr:
                err_lines.append(line.rstrip())
                del err_lines[:-40]
        t = threading.Thread(target=drain, daemon=True)
        t.start()
        start, width = span
        start_wall = time.time()
        runaway = False
        for line in proc.stdout:
            if job.cancelled:
                break
            if line.startswith("out_time_us=") and total_seconds > 0:
                try:
                    secs = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                elapsed = time.time() - start_wall
                speed = secs / elapsed if elapsed > 0.05 else 0.0
                eta = (total_seconds - secs) / speed if speed > 0 else None
                job.progress(start + width * max(0.0, min(1.0, secs / total_seconds)),
                             {"done": secs, "total": total_seconds, "speed": speed, "eta": eta})
                if secs > total_seconds * 2 + 30:
                    runaway = True
                    break
        if job.cancelled or runaway:
            proc.kill()
        proc.wait()
        t.join(timeout=2)
        job.proc = None
        if job.cancelled:
            raise Cancelled("Cancelled.")
        if runaway:
            raise TeaCutError("The file's timestamps are broken, so the result was growing far "
                               "longer than the original. Try Repair with 'Deep repair' ticked.")
        if proc.returncode != 0:
            raise TeaCutError(_summarise_error(err_lines))
        job.progress(start + width, {"done": total_seconds, "total": total_seconds,
                                     "speed": 0.0, "eta": 0.0})

    # -- argument builders --------------------------------------------------
    def copy_args(self, out_ext, audio=True):
        maps = ["-map", "0:V?"]
        if audio:
            maps += ["-map", "0:a?"]
        if out_ext in SUBTITLE_CONTAINERS:
            maps += ["-map", "0:s?"]
        faststart = ["-movflags", "+faststart"] if out_ext in ("mp4", "m4v", "mov") else []
        return maps + ["-c", "copy", "-ignore_unknown"] + faststart

    def reencode_maps(self, out_ext, audio=True):
        """Maps for a re-encode: every audio track, plus subtitles for MKV."""
        maps = ["-map", "0:V:0?"]
        if audio:
            maps += ["-map", "0:a?"]
        if out_ext in SUBTITLE_CONTAINERS:
            maps += ["-map", "0:s?", "-c:s", "copy"]
        return maps

    def encode_args(self, out_ext, video=True, audio=True, video_filter=True, retime_fps=None,
                    quality=None, scale_height=None):
        """retime_fps: rebuild all timestamps from scratch (for badly broken files).
        Pass the frame rate, or 0 if unknown."""
        _, vchoices, achoices = FORMATS.get(out_ext, FORMATS["mkv"])
        args = []
        if video:
            venc = self.pick_encoder(vchoices) or self.pick_encoder(["libx264", "mpeg4"])
            if not venc:
                raise TeaCutError("This FFmpeg build has no usable video encoder.")
            filters = []
            if retime_fps:
                filters.append(f"setpts=N/({retime_fps:g}*TB)")
            if scale_height:
                # -2 keeps the aspect ratio and makes the width even.
                filters.append(f"scale=-2:{scale_height}")
            elif video_filter:
                # Most encoders need even dimensions.
                filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
            if filters:
                args += ["-vf", ",".join(filters)]
            if retime_fps is not None:
                # Never pad gaps with duplicate frames: broken timestamps can
                # otherwise turn a 10-second clip into hours of frozen video.
                args += ["-fps_mode", "cfr" if retime_fps else "vfr"]
            venc_args = list(VIDEO_ENCODER_ARGS.get(venc, []))
            if quality in QUALITY_CRF:
                crf = QUALITY_CRF[quality]
                if "-crf" in venc_args:
                    venc_args[venc_args.index("-crf") + 1] = str(crf)
                    if venc in ("libvpx", "libvpx-vp9") and "-b:v" in venc_args:
                        venc_args[venc_args.index("-b:v") + 1] = "0"
                elif "-q:v" in venc_args:
                    venc_args[venc_args.index("-q:v") + 1] = str(QUALITY_Q[quality])
                elif "-b:v" in venc_args:
                    venc_args[venc_args.index("-b:v") + 1] = QUALITY_BITRATE[quality]
            args += ["-c:v", venc] + venc_args
        if audio:
            aenc = self.pick_encoder(achoices) or self.pick_encoder(["aac", "libmp3lame", "mp2"])
            if aenc:
                if retime_fps is not None:
                    args += ["-af", "asetpts=N/SR/TB"]
                args += ["-c:a", aenc] + AUDIO_ENCODER_ARGS.get(aenc, [])
                if aenc == "libopus":
                    args += ["-ar", "48000"]
                elif aenc in ("mp2", "wmav2", "libmp3lame") or out_ext in ("flv", "3gp"):
                    args += ["-ar", "44100" if out_ext in ("flv", "avi") else "48000", "-ac", "2"]
        extra = CONTAINER_ARGS.get(out_ext, [])
        if out_ext == "avi" and not (video and "mpeg4" in args):
            extra = []
        return args + extra

    def smart_args(self, info, out_ext, mute=False, scale_height=None, quality=None):
        """Copy the streams that already suit out_ext, re-encode only the rest."""
        ok_v, ok_a = NATIVE_CODECS.get(out_ext, (None, None))
        copy_v = (not info.has_video or ok_v is None
                  or all(c in ok_v for c in info.vcodecs))
        if scale_height and info.height > scale_height:
            copy_v = False
        copy_a = (not mute and not info.has_audio or ok_a is None
                  or all(c in ok_a for c in info.acodecs))
        if mute:
            copy_a = False
        if copy_v and (copy_a or mute):
            return self.copy_args(out_ext, audio=not mute), True
        args = self.reencode_maps(out_ext, audio=not mute)
        if info.has_video and copy_v:
            args += ["-c:v", "copy"]
        if info.has_audio and copy_a and not mute:
            args += ["-c:a", "copy"]
        args += self.encode_args(out_ext, info.has_video and not copy_v,
                                 info.has_audio and not copy_a and not mute,
                                 quality=quality, scale_height=scale_height)
        return args, False

    # -- operations ---------------------------------------------------------
    def repair(self, src, dst, job, deep=False):
        info = self.probe(src)
        ext = _ext(dst)
        if not deep:
            job.status("Rebuilding the file structure (lossless)...")
            try:
                self.run(TOLERANT_INPUT + ["-i", src] + self.copy_args(ext) + [dst],
                         job, info.duration)
                if _looks_ok(self, dst):
                    return dst
                job.log("Lossless rebuild produced an unreadable file; trying a full repair.")
            except Cancelled:
                raise
            except TeaCutError as e:
                job.log(f"Lossless rebuild failed: {e}")
        job.status("Repairing by re-encoding (this takes a while)...")
        self.run(TOLERANT_INPUT + ["-i", src] + self.reencode_maps(ext)
                 + self.encode_args(ext, info.has_video, info.has_audio,
                                    retime_fps=info.fps if 1 <= info.fps <= 240 else 0) + [dst],
                 job, info.duration)
        return dst

    def convert(self, src, dst, job, force_encode=False, quality=None, scale_height=None, mute=False):
        info = self.probe(src)
        ext = _ext(dst)
        if mute and not info.has_video:
            raise TeaCutError("There is no video in this file to convert.")
        if scale_height and info.height and info.height <= scale_height:
            scale_height = None  # never upscale
        if not force_encode:
            args, lossless = self.smart_args(info, ext, mute=mute, scale_height=scale_height,
                                             quality=quality)
            job.status("Changing format (lossless)..." if lossless else "Converting...")
            try:
                self.run(["-i", src] + args + [dst], job, info.duration)
                if _looks_ok(self, dst):
                    return dst
            except Cancelled:
                raise
            except TeaCutError as e:
                job.log(f"Lossless change not possible ({e.args[0].splitlines()[0]}); re-encoding.")
        job.status("Converting...")
        self.run(["-i", src] + self.reencode_maps(ext, audio=not mute)
                 + self.encode_args(ext, info.has_video, info.has_audio and not mute,
                                    quality=quality, scale_height=scale_height) + [dst],
                 job, info.duration)
        return dst

    def _cut_piece(self, src, dst, start, end, info, job, exact, span):
        length = end - start
        ext = _ext(dst)
        if not exact:
            try:
                self.run(["-ss", f"{start:.3f}", "-i", src, "-t", f"{length:.3f}"]
                         + self.copy_args(ext) + ["-avoid_negative_ts", "make_zero", dst],
                         job, length, span)
                if _looks_ok(self, dst):
                    return
            except Cancelled:
                raise
            except TeaCutError as e:
                job.log(f"Lossless cut failed ({e.args[0].splitlines()[0]}); re-encoding.")
        self.run(["-ss", f"{start:.3f}", "-i", src, "-t", f"{length:.3f}"] + self.reencode_maps(ext)
                 + self.encode_args(ext, info.has_video, info.has_audio) + [dst],
                 job, length, span)

    def cut(self, src, dst, start, end, job, remove=False, exact=False):
        """Keep [start, end] - or, with remove=True, throw it away and keep the rest."""
        info = self.probe(src)
        dur = info.duration or end
        start = max(0.0, start)
        end = min(end, dur) if dur else end
        if end <= start:
            raise TeaCutError("The end time must be after the start time.")
        if not remove:
            job.status("Cutting..." + (" (frame-exact)" if exact else ""))
            self._cut_piece(src, dst, start, end, info, job, exact, (0.0, 1.0))
            return dst
        pieces = [(a, b) for a, b in ((0.0, start), (end, dur)) if b - a > 0.05]
        if not pieces:
            raise TeaCutError("That would remove the whole video.")
        if len(pieces) == 1:
            job.status("Cutting...")
            self._cut_piece(src, dst, pieces[0][0], pieces[0][1], info, job, exact, (0.0, 1.0))
            return dst
        tmp = tempfile.mkdtemp(prefix=".teacut_", dir=os.path.dirname(os.path.abspath(dst)))
        try:
            parts = []
            total = sum(b - a for a, b in pieces)
            done = 0.0
            for i, (a, b) in enumerate(pieces):
                job.status(f"Cutting part {i + 1} of {len(pieces)}...")
                part = os.path.join(tmp, f"part{i}.{_ext(dst)}")
                span = (0.8 * done / total, 0.8 * (b - a) / total)
                self._cut_piece(src, part, a, b, info, job, exact, span)
                parts.append(part)
                done += b - a
            job.status("Putting the parts back together...")
            try:
                self._concat_copy(parts, dst, job, total, (0.8, 0.2))
            except Cancelled:
                raise
            except TeaCutError:
                # Parts from a keyframe cut can disagree; a re-encoded join always works.
                self._concat_encode([self.probe(p) for p in parts], dst, job, (0.8, 0.2))
            return dst
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def split(self, src, outdir, parts, job, exact=False):
        """Split a file into N equal parts. Returns the list of output paths."""
        info = self.probe(src)
        if parts < 2:
            raise TeaCutError("Split needs at least 2 parts.")
        dur = info.duration
        if not dur or dur <= 0:
            raise TeaCutError("The file's length is unknown, so it can't be split evenly.")
        if dur / parts < 0.1:
            raise TeaCutError("That would make parts shorter than 0.1 seconds.")
        ext = _ext(src)
        base = os.path.splitext(os.path.basename(src))[0]
        paths = []
        for i in range(parts):
            a = dur * i / parts
            b = dur * (i + 1) / parts
            path = unique_path(os.path.join(outdir, f"{base}_part{i + 1:02d}.{ext}"))
            job.status(f"Splitting part {i + 1} of {parts}...")
            self._cut_piece(src, path, a, b, info, job, exact, (i / parts, 1 / parts))
            paths.append(path)
        return paths

    def join(self, sources, dst, job, force_encode=False):
        if len(sources) < 2:
            raise TeaCutError("Add at least two files to join.")
        job.status("Checking files...")
        infos = [self.probe(s) for s in sources]
        total = sum(i.duration for i in infos)
        same = len({i.signature() for i in infos}) == 1
        if same and not force_encode:
            job.status("Joining (lossless)...")
            try:
                self._concat_copy(sources, dst, job, total, (0.0, 1.0))
                if _looks_ok(self, dst):
                    return dst
            except Cancelled:
                raise
            except TeaCutError as e:
                job.log(f"Lossless join failed ({e.args[0].splitlines()[0]}); re-encoding.")
        elif not same:
            job.log("Files differ in size or format, so they will be re-encoded to match.")
        job.status("Joining (re-encoding so everything matches)...")
        self._concat_encode(infos, dst, job, (0.0, 1.0))
        return dst

    def _concat_copy(self, files, dst, job, total, span):
        fd, listfile = tempfile.mkstemp(suffix=".txt", prefix="teacut_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for p in files:
                    f.write("file '" + os.path.abspath(p).replace("'", "'\\''") + "'\n")
            self.run(["-f", "concat", "-safe", "0", "-i", listfile] + self.copy_args(_ext(dst)) + [dst],
                     job, total, span)
        finally:
            os.remove(listfile)

    def _concat_encode(self, infos, dst, job, span):
        ext = _ext(dst)
        first_video = next((i for i in infos if i.has_video), None)
        any_audio = any(i.has_audio for i in infos)
        w = (first_video.width if first_video and first_video.width else 1280) // 2 * 2
        h = (first_video.height if first_video and first_video.height else 720) // 2 * 2
        fps = first_video.fps if first_video and 1 <= first_video.fps <= 120 else 30
        fps = f"{fps:.3f}".rstrip("0").rstrip(".")
        inputs, chains, labels = [], [], []
        n_in = 0
        for k, info in enumerate(infos):
            dur = f"{max(info.duration, 0.1):.3f}"
            inputs += ["-i", info.path]
            idx = n_in
            n_in += 1
            if info.has_video:
                vsrc = f"[{idx}:v:0]"
            else:
                inputs += ["-f", "lavfi", "-t", dur, "-i", f"color=c=black:s={w}x{h}:r={fps}"]
                vsrc = f"[{n_in}:v]"
                n_in += 1
            chains.append(f"{vsrc}scale={w}:{h}:force_original_aspect_ratio=decrease,"
                          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{k}]")
            labels.append(f"[v{k}]")
            if any_audio:
                if info.has_audio:
                    asrc = f"[{idx}:a:0]"
                else:
                    inputs += ["-f", "lavfi", "-t", dur, "-i", "anullsrc=r=48000:cl=stereo"]
                    asrc = f"[{n_in}:a]"
                    n_in += 1
                chains.append(f"{asrc}aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{k}]")
                labels.append(f"[a{k}]")
        graph = ";".join(chains) + ";" + "".join(labels) + \
            f"concat=n={len(infos)}:v=1:a={1 if any_audio else 0}[vout]" + ("[aout]" if any_audio else "")
        maps = ["-map", "[vout]"] + (["-map", "[aout]"] if any_audio else [])
        total = sum(i.duration for i in infos)
        self.run(inputs + ["-filter_complex", graph] + maps
                 + self.encode_args(ext, True, any_audio, video_filter=False) + [dst],
                 job, total, span)


def _looks_ok(engine, path):
    """A stream copy can 'succeed' yet write nothing useful; check the result."""
    if not os.path.isfile(path) or os.path.getsize(path) < 1024:
        return False
    try:
        engine.probe(path)
        return True
    except TeaCutError:
        return False


def _summarise_error(lines):
    keep = [l for l in lines if l and not l.startswith(("  ", "Input #", "Output #", "Stream mapping"))]
    text = "\n".join(keep[-6:]) or "FFmpeg failed for an unknown reason."
    return text


def _quote(a):
    return f'"{a}"' if (" " in a or not a) else a


def _ext(path):
    return os.path.splitext(path)[1].lstrip(".").lower() or "mkv"


def ensure_parent(path):
    """Create the folder a result will be written to (if it isn't there yet)."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers shared by GUI and CLI
# ---------------------------------------------------------------------------

def parse_time(text):
    """Accepts 90, 1:30, 01:02:03.5, 1m30s, 1h2m3s."""
    t = str(text).strip().lower().replace(",", ".")
    if not t:
        raise ValueError("empty time")
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+(?:\.\d+)?)m)?\s*(?:(\d+(?:\.\d+)?)s?)?", t)
    if m and any(m.groups()) and not re.fullmatch(r"\d+(?:\.\d+)?", t):
        h, mi, s = (float(g) if g else 0.0 for g in m.groups())
        return h * 3600 + mi * 60 + s
    parts = t.split(":")
    if len(parts) > 3 or not all(re.fullmatch(r"\d+(?:\.\d+)?", p) for p in parts):
        raise ValueError(f"'{text}' is not a time (try 1:30 or 01:02:03)")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def format_time(secs):
    """Format seconds as 1:02:03.5 (or 2:03.5 under an hour)."""
    # Round to whole milliseconds first: formatting the float directly can
    # turn 59.9999 into "0:60" and 3599.9999 into "59:60".
    total_ms = round(max(0.0, secs) * 1000)
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    if ms:
        s_txt = f"{s:02d}.{ms:03d}".rstrip("0").rstrip(".")
    else:
        s_txt = f"{s:02d}"
    return f"{h}:{m:02d}:{s_txt}" if h else f"{m}:{s_txt}"


def unique_path(path):
    base, ext = os.path.splitext(path)
    n, candidate = 2, path
    while os.path.exists(candidate):
        candidate = f"{base} ({n}){ext}"
        n += 1
    return candidate


def suggest_output(src, tag, ext=None, outdir=None):
    """A free '<name>_<tag>.<ext>' path, next to the source or in outdir."""
    base, src_ext = os.path.splitext(src)
    name = f"{os.path.basename(base)}_{tag}.{ext or src_ext.lstrip('.') or 'mkv'}"
    folder = outdir or os.path.dirname(os.path.abspath(src))
    return unique_path(os.path.join(folder, name))


def resolve_output(out, src, tag, outdir=None):
    """Turn a CLI -o value into a path, honouring --output-dir for bare names."""
    if out:
        if outdir and not os.path.dirname(out):
            return os.path.join(outdir, out)
        return out
    return suggest_output(src, tag, None, outdir)


class Job:
    """Progress/cancel plumbing between the engine and whoever is watching."""

    def __init__(self, on_progress=None, on_status=None, on_log=None):
        self.proc = None
        self.cancelled = False
        self._p, self._s, self._l = on_progress, on_status, on_log

    def progress(self, frac, info=None):
        if self._p:
            self._p(frac, info)

    def status(self, text):
        if self._s:
            self._s(text)

    def log(self, text):
        if self._l:
            self._l(text)

    def cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def cli(argv):
    p = argparse.ArgumentParser(prog="teacut", description=f"{APP_NAME} {VERSION} - repair, cut and join videos.")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("info", help="show what is inside a file")
    s.add_argument("file")
    s = sub.add_parser("repair", help="fix a broken or unplayable file")
    s.add_argument("file")
    s.add_argument("-o", "--output", help="output file (default: next to the original)")
    s.add_argument("--output-dir", metavar="DIR", help="save the result in this folder (created if needed)")
    s.add_argument("--deep", action="store_true", help="re-encode instead of trying a lossless rebuild first")
    s = sub.add_parser("cut", help="keep (or remove) a section")
    s.add_argument("file")
    s.add_argument("--start", default="0")
    s.add_argument("--end", required=True)
    s.add_argument("--remove", action="store_true", help="remove the section instead of keeping it")
    s.add_argument("--exact", action="store_true", help="frame-exact (re-encodes)")
    s.add_argument("-o", "--output", help="output file (default: next to the original)")
    s.add_argument("--output-dir", metavar="DIR", help="save the result in this folder (created if needed)")
    s = sub.add_parser("join", help="join files end to end")
    s.add_argument("files", nargs="+")
    s.add_argument("-o", "--output", help="output file (default: next to the first input)")
    s.add_argument("--output-dir", metavar="DIR", help="save the result in this folder (created if needed)")
    s.add_argument("--reencode", action="store_true")
    s = sub.add_parser("convert", help="change format")
    s.add_argument("file")
    s.add_argument("-o", "--output", required=True, help="output file; its extension picks the format")
    s.add_argument("--output-dir", metavar="DIR", help="save a bare -o filename in this folder")
    s.add_argument("--quality", choices=["smaller", "balanced", "high"],
                   help="output quality (default: balanced)")
    s.add_argument("--scale", type=int, metavar="HEIGHT",
                   help="downscale to this height (1080, 720, 480); never upscales")
    s.add_argument("--mute", action="store_true", help="drop the audio")
    s.add_argument("--reencode", action="store_true")
    s = sub.add_parser("split", help="split a file into equal parts")
    s.add_argument("file")
    s.add_argument("--parts", type=int, default=2, help="number of parts (default: 2)")
    s.add_argument("--exact", action="store_true", help="frame-exact (re-encodes)")
    s.add_argument("--output-dir", metavar="DIR",
                   help="save the parts in this folder (default: next to the source)")
    a = p.parse_args(argv)

    def show(frac, info=None):
        text = f"\r{frac * 100:5.1f}%"
        if info:
            eta = info.get("eta")
            speed = info.get("speed") or 0.0
            if eta:
                text += f"  {format_time(eta)} left"
            if speed >= 0.1:
                text += f"  {speed:.1f}x"
        sys.stderr.write(text)
        sys.stderr.flush()
    job = Job(show, lambda t: sys.stderr.write(f"\n{t}\n"),
              (lambda t: sys.stderr.write(f"\n  {t}\n")) if os.environ.get("TEACUT_DEBUG") else None)
    try:
        eng = Engine()
        if a.cmd == "info":
            i = eng.probe(a.file)
            print(f"{a.file}\n  container: {i.container}\n  {i.summary()}")
            return 0
        if a.cmd == "repair":
            out = eng.repair(a.file, resolve_output(a.output, a.file, "repaired", a.output_dir), job, a.deep)
        elif a.cmd == "cut":
            out = eng.cut(a.file, resolve_output(a.output, a.file, "cut", a.output_dir),
                          parse_time(a.start), parse_time(a.end), job, a.remove, a.exact)
        elif a.cmd == "split":
            outdir = a.output_dir or os.path.dirname(os.path.abspath(a.file))
            outs = eng.split(a.file, outdir, a.parts, job, a.exact)
            sys.stderr.write("\n")
            for out in outs:
                print(f"Saved: {out}")
            return 0
        elif a.cmd == "join":
            out = eng.join(a.files, resolve_output(a.output, a.files[0], "joined", a.output_dir),
                           job, a.reencode)
        else:
            out = eng.convert(a.file, resolve_output(a.output, a.file, "converted", a.output_dir),
                              job, a.reencode, quality=a.quality, scale_height=a.scale, mute=a.mute)
        sys.stderr.write("\n")
        print(f"Saved: {out}")
        return 0
    except (TeaCutError, ValueError) as e:
        sys.stderr.write(f"\nError: {e}\n")
        return 1
    except KeyboardInterrupt:
        job.cancel()
        return 130


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

def _settings_path():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "TeaCut", "settings.json")


def load_settings():
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    try:
        os.makedirs(os.path.dirname(_settings_path()), exist_ok=True)
        with open(_settings_path(), "w", encoding="utf-8") as f:
            json.dump(settings, f)
    except OSError:
        pass


def system_prefers_dark():
    if sys.platform.startswith("win"):
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        except OSError:
            return False
    if sys.platform == "darwin":
        return "Dark" in _run_quiet(["defaults", "read", "-g", "AppleInterfaceStyle"]).stdout
    if sys.platform.startswith("linux"):
        # GNOME / systemd-based desktops.
        try:
            out = _run_quiet(["gsettings", "get", "org.gnome.desktop.interface",
                              "color-scheme"]).stdout
            if "dark" in out.lower():
                return True
        except OSError:
            pass
        # GTK 3 fallback.
        try:
            path = os.path.join(os.path.expanduser("~"), ".config", "gtk-3.0", "settings.ini")
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip().lower().startswith("gtk-application-prefer-dark-theme"):
                        return "true" in line.lower()
        except OSError:
            pass
    return False


def gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    if sys.platform.startswith("win"):
        try:  # render crisply on high-DPI screens instead of being bitmap-stretched
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        root = TkinterDnD.Tk()
    except Exception:
        TkinterDnD = DND_FILES = None
        root = tk.Tk()

    root.title(f"{APP_NAME} {VERSION}")
    icons = [os.path.join(_app_dir(), "assets", n) for n in ("teacut.png", "teacut-small.png")]
    try:
        root.iconphoto(True, *[tk.PhotoImage(file=i) for i in icons if os.path.isfile(i)])
    except (tk.TclError, TypeError):
        pass

    try:
        root.tk.call("tk", "scaling", max(1.0, root.winfo_fpixels("1i") / 72.0))
    except tk.TclError:
        pass
    style = ttk.Style()
    try:
        import sv_ttk  # Windows 11 style light/dark theme
    except ImportError:
        sv_ttk = None
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "aqua" not in style.theme_names() and "clam" in style.theme_names():
            style.theme_use("clam")
    big_style = "Big.Accent.TButton" if sv_ttk else "Big.TButton"
    settings = load_settings()
    dark_mode = tk.BooleanVar(value=settings.get("dark", system_prefers_dark()))
    theme_hooks = []  # extra recolouring for plain-Tk widgets

    def set_title_bar(dark):
        """Ask Windows 10/11 to draw a dark title bar to match."""
        if not sys.platform.startswith("win") or not root.winfo_ismapped():
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            value = ctypes.c_int(1 if dark else 0)
            for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new, old builds)
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
            w, h = root.winfo_width(), root.winfo_height()
            root.geometry(f"{w + 1}x{h}")  # nudge so the title bar repaints now
            root.geometry(f"{w}x{h}")
        except Exception:
            pass

    def apply_theme():
        dark = dark_mode.get()
        if sv_ttk:
            sv_ttk.set_theme("dark" if dark else "light")
        else:
            style.configure("TNotebook.Tab", padding=(16, 6))
        # Styles belong to a theme, so these are re-applied after every switch.
        style.configure(big_style, font=("TkDefaultFont", 11, "bold"), padding=(28, 8))
        for hook in theme_hooks:
            hook(dark)
        set_title_bar(dark)

    def toggle_theme():
        settings["dark"] = dark_mode.get()
        save_settings(settings)
        apply_theme()

    try:
        engine = Engine()
    except TeaCutError as e:
        messagebox.showerror(APP_NAME, str(e))
        root.destroy()
        return

    def wrapping_label(parent, hint=False, **kw):
        label = ttk.Label(parent, justify="left", **kw)
        if hint:
            # Set on the widget itself: the Sun Valley theme ignores a style's text colour.
            theme_hooks.append(lambda dark: label.configure(
                foreground=("#a3a3a3" if dark else "#5c5c5c") if sv_ttk else "#666"))
        label.bind("<Configure>", lambda e: label.configure(wraplength=max(200, e.width - 4)))
        return label

    events = queue.Queue()
    state = {"job": None, "last_output": None}
    filetypes = [("Video files", " ".join(f"*.{e}" for e in VIDEO_EXTENSIONS)
                  + " " + " ".join(f"*.{e.upper()}" for e in VIDEO_EXTENSIONS)),
                 ("All files", "*.*")]

    def destination(src, tag, ext=None):
        """Where a result goes: the chosen folder, or next to the source."""
        folder = outdir_var.get().strip() if use_outdir.get() else None
        return suggest_output(src, tag, ext, folder)

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=10, pady=(10, 0))

    # -- shared bottom bar ----------------------------------------------------
    bottom = ttk.Frame(root, padding=10)
    bottom.pack(fill="x")

    # Where results go: next to the original, or a folder the user picked.
    saved_outdir = settings.get("outdir") or ""
    outdir_var = tk.StringVar(value=saved_outdir)
    use_outdir = tk.BooleanVar(value=bool(saved_outdir))

    def choose_outdir():
        path = filedialog.askdirectory(title="Save results in", mustexist=False)
        if not path:
            return
        outdir_var.set(path)
        use_outdir.set(True)
        settings["outdir"] = path
        save_settings(settings)

    def on_use_outdir():
        if use_outdir.get():
            if not outdir_var.get().strip():
                choose_outdir()
            if use_outdir.get() and outdir_var.get().strip():
                settings["outdir"] = outdir_var.get().strip()
                save_settings(settings)
            else:
                use_outdir.set(False)
        else:
            settings.pop("outdir", None)
            save_settings(settings)

    out_row = ttk.Frame(bottom)
    out_row.pack(fill="x", pady=(0, 2))
    ttk.Checkbutton(out_row, text="Save results in:", variable=use_outdir,
                    command=on_use_outdir).pack(side="left")
    ttk.Label(out_row, textvariable=outdir_var, width=38, anchor="w",
              font=("TkDefaultFont", 9)).pack(side="left", padx=(4, 0))
    ttk.Button(out_row, text="Choose folder...", command=choose_outdir).pack(side="left", padx=(6, 0))

    status = tk.StringVar(value="Pick a tab, choose a file, press the big button.")
    wrapping_label(bottom, textvariable=status).pack(fill="x")
    bar_row = ttk.Frame(bottom)
    bar_row.pack(fill="x", pady=6)
    bar = ttk.Progressbar(bar_row, maximum=1000)
    bar.pack(side="left", fill="x", expand=True)
    progress_var = tk.StringVar(value="")
    ttk.Label(bar_row, textvariable=progress_var, width=24, anchor="e",
              font=("TkDefaultFont", 9)).pack(side="left", padx=(8, 0))

    # A small log window makes FFmpeg failures debuggable without a console.
    log_lines = []
    log_dialog = {"win": None, "text": None}

    def show_details():
        win, text = log_dialog["win"], log_dialog["text"]
        if win is not None:
            try:
                if win.winfo_exists():
                    win.focus()
                    return
            except tk.TclError:
                pass
        win = tk.Toplevel(root)
        win.title(f"{APP_NAME} - details")
        win.geometry("760x420")
        text = tk.Text(win, wrap="word", height=20, width=90)
        text.pack(fill="both", expand=True, padx=8, pady=8)
        dark = dark_mode.get()
        text.configure(background="#2b2b2b" if dark else "#ffffff",
                       foreground="#fafafa" if dark else "#1c1c1c",
                       insertbackground="#fafafa" if dark else "#1c1c1c")
        text.insert("1.0", "\n".join(log_lines[-400:]))
        text.configure(state="disabled")
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 8))
        log_dialog["win"], log_dialog["text"] = win, text

    btns = ttk.Frame(bottom)
    btns.pack(fill="x")
    cancel_btn = ttk.Button(btns, text="Cancel", state="disabled",
                            command=lambda: state["job"] and state["job"].cancel())
    cancel_btn.pack(side="left")
    ttk.Button(btns, text="Details", command=show_details).pack(side="left", padx=(8, 0))
    open_btn = ttk.Button(btns, text="Show result in folder", state="disabled",
                          command=lambda: reveal(state["last_output"]))
    open_btn.pack(side="right")
    if sv_ttk:
        ttk.Checkbutton(btns, text="Dark mode", style="Switch.TCheckbutton",
                        variable=dark_mode, command=toggle_theme).pack(side="left", expand=True)

    start_buttons = []

    def reveal(path):
        if not path:
            return
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except OSError as e:
            messagebox.showerror(APP_NAME, str(e))

    def start(work, label):
        """Run work(job) on a background thread; it returns a path or a list of paths."""
        if state["job"]:
            return
        job = Job(lambda f, info=None: events.put(("progress", (f, info))),
                  lambda t: events.put(("status", t)),
                  lambda t: events.put(("log", t)))
        state["job"] = job
        for b in start_buttons:
            b.state(["disabled"])
        cancel_btn.state(["!disabled"])
        open_btn.state(["disabled"])
        bar["value"] = 0
        progress_var.set("")
        status.set(label)
        log_lines.clear()
        win, text = log_dialog["win"], log_dialog["text"]
        if win is not None and text is not None:
            try:
                if win.winfo_exists():
                    text.configure(state="normal")
                    text.delete("1.0", "end")
                    text.configure(state="disabled")
            except tk.TclError:
                pass

        def worker():
            try:
                events.put(("done", work(job)))
            except Cancelled:
                events.put(("cancelled", None))
            except Exception as e:  # report anything, never die silently
                events.put(("error", str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def pump():
        try:
            while True:
                kind, val = events.get_nowait()
                if kind == "progress":
                    frac, info = val
                    bar["value"] = frac * 1000
                    if info:
                        eta = info.get("eta")
                        speed = info.get("speed") or 0.0
                        bits = []
                        if eta:
                            bits.append(f"{format_time(eta)} left")
                        if speed >= 0.1:
                            bits.append(f"{speed:.1f}x")
                        progress_var.set(" · ".join(bits))
                    else:
                        progress_var.set("")
                elif kind == "status":
                    status.set(val)
                elif kind == "log":
                    log_lines.append(val)
                    if len(log_lines) > 500:
                        del log_lines[:-500]
                    win, text = log_dialog["win"], log_dialog["text"]
                    if win is not None and text is not None:
                        try:
                            if win.winfo_exists():
                                text.configure(state="normal")
                                text.insert("end", val + "\n")
                                text.see("end")
                                text.configure(state="disabled")
                        except tk.TclError:
                            pass
                elif kind == "batch_status":
                    src, text = val
                    batch_statuses[src] = text
                    refresh_batch()
                else:
                    job_out = state["job"]
                    state["job"] = None
                    for b in start_buttons:
                        b.state(["!disabled"])
                    cancel_btn.state(["disabled"])
                    progress_var.set("")
                    if kind == "done":
                        bar["value"] = 1000
                        if isinstance(val, (list, tuple)):
                            if val:
                                state["last_output"] = val[0]
                                open_btn.state(["!disabled"])
                                status.set(f"Done! Saved {len(val)} files to {os.path.dirname(val[0])}")
                            else:
                                state["last_output"] = None
                                status.set("Done! Nothing was written.")
                        else:
                            state["last_output"] = val
                            open_btn.state(["!disabled"])
                            status.set(f"Done! Saved to {val}")
                    elif kind == "cancelled":
                        bar["value"] = 0
                        status.set("Cancelled.")
                    else:
                        bar["value"] = 0
                        status.set("Something went wrong.")
                        messagebox.showerror(APP_NAME, val)
                    del job_out
        except queue.Empty:
            pass
        root.after(80, pump)
    root.after(80, pump)

    def big_button(parent, text, cmd):
        b = ttk.Button(parent, text=text, style=big_style, command=cmd)
        b.pack(pady=(18, 4))
        start_buttons.append(b)
        return b

    def file_picker(parent, on_change):
        """A 'Choose video' row with a small preview. Returns (get_path, set_path)."""
        holder = {"path": None, "info": None, "image": None}
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(16, 2))
        name = tk.StringVar(value="No file chosen" + (" - or drop one here" if DND_FILES else ""))
        details = tk.StringVar(value="Works with MP4, MKV, AVI, MOV, WMV, ASF, FLV, WebM, MPEG, TS, 3GP and more.")
        thumb = ttk.Label(row, text="")
        thumb_path = os.path.join(tempfile.gettempdir(), f"teacut_thumb_{id(holder)}.png")

        def set_path(path):
            try:
                info = engine.probe(path)
            except TeaCutError as e:
                messagebox.showerror(APP_NAME, str(e))
                return
            holder["path"], holder["info"] = path, info
            name.set(os.path.basename(path))
            details.set(info.summary())
            try:
                os.remove(thumb_path)
            except OSError:
                pass
            if info.has_video and engine.ffmpeg:
                at = min(1.0, info.duration / 2) if info.duration else 0.0
                extract_frame(engine.ffmpeg, path, thumb_path, at)
            try:
                if os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 100:
                    img = tk.PhotoImage(file=thumb_path)
                    holder["image"] = img
                    thumb.configure(image=img, text="")
                else:
                    holder["image"] = None
                    thumb.configure(image="", text="no preview")
            except tk.TclError:
                holder["image"] = None
                thumb.configure(image="", text="no preview")
            on_change(info)

        def choose():
            path = filedialog.askopenfilename(title="Choose a video", filetypes=filetypes)
            if path:
                set_path(path)
        ttk.Button(row, text="Choose video...", command=choose).pack(side="left")
        ttk.Label(row, textvariable=name, font=("TkDefaultFont", 10, "bold")).pack(side="left", padx=10)
        thumb.pack(side="right", padx=(10, 0))
        wrapping_label(parent, textvariable=details, hint=True).pack(fill="x")
        return holder, set_path

    def tab(title, hint):
        f = ttk.Frame(notebook, padding=14)
        notebook.add(f, text=title)
        wrapping_label(f, text=hint, hint=True).pack(fill="x", pady=(0, 8))
        return f

    def need(holder):
        if not holder["path"]:
            messagebox.showinfo(APP_NAME, "Choose a video first.")
            return False
        return True

    # -- Cut ----------------------------------------------------------------
    t_cut = tab("  Cut  ", "Keep just part of a video, remove a part, or split it into equal pieces. "
                           "Times can be written like 90, 1:30 or 1:02:03, and \"end\" means the end of the video.")
    times = ttk.Frame(t_cut)
    start_var, end_var = tk.StringVar(value="0:00"), tk.StringVar(value="end")

    def on_cut_file(info):
        start_var.set("0:00")
        end_var.set(format_time(info.duration) if info.duration else "end")
    times.pack(fill="x", pady=(0, 10))
    ttk.Label(times, text="From").pack(side="left")
    ttk.Entry(times, textvariable=start_var, width=12).pack(side="left", padx=6)
    ttk.Label(times, text="to").pack(side="left")
    ttk.Entry(times, textvariable=end_var, width=12).pack(side="left", padx=6)
    mode = tk.StringVar(value="keep")
    ttk.Radiobutton(t_cut, text="Keep this part", variable=mode, value="keep").pack(anchor="w")
    ttk.Radiobutton(t_cut, text="Remove this part (keep the rest)", variable=mode, value="remove").pack(anchor="w")
    ttk.Radiobutton(t_cut, text="Split into equal parts", variable=mode, value="split").pack(anchor="w")
    parts_row = ttk.Frame(t_cut)
    parts_row.pack(fill="x", pady=(4, 0))
    ttk.Label(parts_row, text="Number of parts:").pack(side="left")
    parts_var = tk.StringVar(value="2")
    parts_entry = ttk.Entry(parts_row, textvariable=parts_var, width=6)
    parts_entry.pack(side="left", padx=6)

    def on_mode(*_):
        try:
            parts_entry.configure(state="normal" if mode.get() == "split" else "disabled")
        except tk.TclError:
            pass
    mode.trace_add("write", on_mode)
    on_mode()

    exact = tk.BooleanVar(value=False)
    ttk.Checkbutton(t_cut, text="Frame-exact (slower, re-encodes)", variable=exact).pack(anchor="w", pady=(8, 0))
    wrapping_label(t_cut, hint=True,
                   text="Left off, cuts are instant and lossless but start at the nearest keyframe, "
                        "which can be up to a few seconds early. Split writes one file per part.").pack(fill="x", pady=(4, 0))
    cut_file, cut_set = file_picker(t_cut, on_cut_file)

    def do_cut():
        if not need(cut_file):
            return
        src = cut_file["path"]
        if mode.get() == "split":
            try:
                parts = int(parts_var.get())
            except ValueError:
                messagebox.showerror(APP_NAME, "Enter a whole number of parts.")
                return
            if parts < 2:
                messagebox.showerror(APP_NAME, "Split needs at least 2 parts.")
                return
            outdir = outdir_var.get().strip() if use_outdir.get() else os.path.dirname(os.path.abspath(src))
            is_exact = exact.get()
            start(lambda job: engine.split(src, outdir, parts, job, is_exact), "Splitting...")
            return
        try:
            a = parse_time(start_var.get())
            end_text = end_var.get().strip().lower()
            b = (cut_file["info"].duration or 1e7) if end_text in ("", "end") else parse_time(end_text)
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        dst = destination(src, "cut")
        remove = mode.get() == "remove"
        is_exact = exact.get()
        start(lambda job: engine.cut(src, dst, a, b, job, remove, is_exact), "Cutting...")
    big_button(t_cut, "Cut", do_cut)

    # -- Join ---------------------------------------------------------------
    t_join = tab("  Join  ", "Join several videos end to end, in the order shown. Matching files "
                             "join instantly; mismatched ones are converted to match the first.")
    lst_frame = ttk.Frame(t_join)
    lst_frame.pack(fill="both", expand=True)
    listbox = tk.Listbox(lst_frame, height=7, activestyle="none", selectmode="browse",
                         relief="flat", borderwidth=0, highlightthickness=1)
    theme_hooks.append(lambda dark: listbox.configure(
        background="#2b2b2b" if dark else "#fdfdfd", foreground="#fafafa" if dark else "#1c1c1c",
        selectbackground="#57c8ff" if dark else "#005fb8", selectforeground="#000" if dark else "#fff",
        highlightbackground="#3c3c3c" if dark else "#d6d6d6", highlightcolor="#57c8ff" if dark else "#005fb8"))
    listbox.pack(side="left", fill="both", expand=True)
    join_files = []
    join_infos = []
    side = ttk.Frame(lst_frame)
    side.pack(side="left", fill="y", padx=(8, 0))

    def refresh():
        listbox.delete(0, "end")
        for p, info in zip(join_files, join_infos):
            label = os.path.basename(p)
            if info and info.duration:
                label += f"  ({format_time(info.duration)})"
            listbox.insert("end", label)

    def add_files(paths=None):
        paths = paths or filedialog.askopenfilenames(title="Add videos", filetypes=filetypes)
        for p in paths:
            try:
                info = engine.probe(p)
            except TeaCutError as e:
                messagebox.showerror(APP_NAME, str(e))
                continue
            join_files.append(p)
            join_infos.append(info)
        refresh()

    def add_folder():
        folder = filedialog.askdirectory(title="Add all videos in a folder")
        if not folder:
            return
        def natural(name):
            # part2 before part10
            return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]
        paths = sorted((os.path.join(folder, name) for name in os.listdir(folder)
                        if os.path.splitext(name)[1].lstrip(".").lower() in VIDEO_EXTENSIONS),
                       key=lambda p: natural(os.path.basename(p)))
        if not paths:
            messagebox.showinfo(APP_NAME, "No videos found in that folder.")
            return
        add_files(paths)

    def move(delta):
        sel = listbox.curselection()
        if not sel:
            return
        i, j = sel[0], sel[0] + delta
        if 0 <= j < len(join_files):
            join_files[i], join_files[j] = join_files[j], join_files[i]
            join_infos[i], join_infos[j] = join_infos[j], join_infos[i]
            refresh()
            listbox.selection_set(j)

    def remove_sel():
        sel = listbox.curselection()
        if sel:
            join_files.pop(sel[0])
            join_infos.pop(sel[0])
            refresh()

    def clear_all():
        join_files.clear()
        join_infos.clear()
        refresh()
    ttk.Button(side, text="Add...", command=add_files).pack(fill="x")
    ttk.Button(side, text="Add folder...", command=add_folder).pack(fill="x", pady=4)
    ttk.Button(side, text="Remove", command=remove_sel).pack(fill="x")
    ttk.Button(side, text="Move up", command=lambda: move(-1)).pack(fill="x", pady=4)
    ttk.Button(side, text="Move down", command=lambda: move(1)).pack(fill="x")
    ttk.Button(side, text="Clear", command=clear_all).pack(fill="x", pady=4)

    def do_join():
        if len(join_files) < 2:
            messagebox.showinfo(APP_NAME, "Add at least two videos.")
            return
        files = list(join_files)
        exts = {_ext(p) for p in files}
        dst = destination(files[0], "joined", exts.pop() if len(exts) == 1 else "mp4")
        start(lambda job: engine.join(files, dst, job), "Joining...")
    big_button(t_join, "Join", do_join)

    # -- Repair -------------------------------------------------------------
    t_rep = tab("  Repair  ", "Fixes videos that won't open, can't seek, show the wrong length, "
                              "stutter, or stop early (broken index, bad timestamps, damaged data).")
    deep = tk.BooleanVar(value=False)
    ttk.Checkbutton(t_rep, text="Deep repair (slower, re-encodes)",
                    variable=deep).pack(anchor="w", pady=(12, 0))
    wrapping_label(t_rep, hint=True,
                   text="Quick fix (default): rebuilds the file's structure without touching the picture "
                        "or sound. Takes seconds and loses no quality.\n"
                        "Deep repair: decodes everything that can still be read and re-creates the video "
                        "with fresh timing. For files that still misbehave after the quick fix.").pack(fill="x", pady=(10, 0))
    rep_file, rep_set = file_picker(t_rep, lambda info: None)

    def do_repair():
        if not need(rep_file):
            return
        src = rep_file["path"]
        ext = _ext(src)
        # Damaged MP4/MOV are safest rebuilt into MKV, which tolerates gaps.
        dst = destination(src, "repaired", ext if ext in FORMATS or ext in ("wmv", "asf") else "mkv")
        is_deep = deep.get()
        start(lambda job: engine.repair(src, dst, job, is_deep), "Repairing...")
    big_button(t_rep, "Repair", do_repair)

    # -- Convert ------------------------------------------------------------
    t_conv = tab("  Convert  ", "Change a video to another format. If the video can simply be moved "
                                "into the new format, that's instant and lossless; otherwise it's converted.")
    fmt_row = ttk.Frame(t_conv)
    fmt_row.pack(fill="x", pady=(0, 4))
    ttk.Label(fmt_row, text="Make it:").pack(side="left")
    labels = [f"{label}  (.{ext})" for ext, (label, _, _) in FORMATS.items()]
    fmt = tk.StringVar(value=labels[0])
    ttk.Combobox(fmt_row, textvariable=fmt, values=labels, state="readonly", width=42).pack(side="left", padx=8)
    opt_row = ttk.Frame(t_conv)
    opt_row.pack(fill="x", pady=(0, 4))
    ttk.Label(opt_row, text="Quality:").pack(side="left")
    quality = tk.StringVar(value="Balanced")
    ttk.Combobox(opt_row, textvariable=quality, values=["Smaller", "Balanced", "High"],
                 state="readonly", width=10).pack(side="left", padx=6)
    ttk.Label(opt_row, text="Size:").pack(side="left", padx=(8, 0))
    height = tk.StringVar(value="Original")
    ttk.Combobox(opt_row, textvariable=height, values=["Original", "1080p", "720p", "480p"],
                 state="readonly", width=10).pack(side="left", padx=6)
    mute = tk.BooleanVar(value=False)
    ttk.Checkbutton(opt_row, text="Mute audio", variable=mute).pack(side="left", padx=(8, 0))
    conv_file, conv_set = file_picker(t_conv, lambda info: None)

    def do_convert():
        if not need(conv_file):
            return
        ext = re.search(r"\(\.(\w+)\)$", fmt.get()).group(1)
        src = conv_file["path"]
        dst = destination(src, "converted", ext)
        quality_map = {"Smaller": "smaller", "Balanced": "balanced", "High": "high"}
        height_map = {"Original": None, "1080p": 1080, "720p": 720, "480p": 480}
        q = quality_map[quality.get()]
        h = height_map[height.get()]
        m = mute.get()
        start(lambda job: engine.convert(src, dst, job, quality=q, scale_height=h, mute=m),
              "Converting...")
    big_button(t_conv, "Convert", do_convert)

    # -- Batch --------------------------------------------------------------
    t_batch = tab("  Batch  ", "Convert or repair many files one after another. Results go to the "
                              "folder chosen at the bottom, or next to each source.")
    lst_frame = ttk.Frame(t_batch)
    lst_frame.pack(fill="both", expand=True)
    batch_list = tk.Listbox(lst_frame, height=8, activestyle="none", selectmode="browse",
                            relief="flat", borderwidth=0, highlightthickness=1)
    theme_hooks.append(lambda dark: batch_list.configure(
        background="#2b2b2b" if dark else "#fdfdfd", foreground="#fafafa" if dark else "#1c1c1c",
        selectbackground="#57c8ff" if dark else "#005fb8", selectforeground="#000" if dark else "#fff",
        highlightbackground="#3c3c3c" if dark else "#d6d6d6", highlightcolor="#57c8ff" if dark else "#005fb8"))
    batch_list.pack(side="left", fill="both", expand=True)
    batch_side = ttk.Frame(lst_frame)
    batch_side.pack(side="left", fill="y", padx=(8, 0))
    batch_files = []
    batch_statuses = {}

    def refresh_batch():
        batch_list.delete(0, "end")
        for p in batch_files:
            batch_list.insert("end", f"{os.path.basename(p)}  —  {batch_statuses.get(p, 'ready')}")

    def batch_add(paths=None):
        paths = paths or filedialog.askopenfilenames(title="Add videos", filetypes=filetypes)
        for p in paths:
            if p in batch_files:
                continue
            try:
                engine.probe(p)
            except TeaCutError as e:
                messagebox.showerror(APP_NAME, str(e))
                continue
            batch_files.append(p)
            batch_statuses[p] = "ready"
        refresh_batch()

    def batch_add_folder():
        folder = filedialog.askdirectory(title="Add all videos in a folder")
        if not folder:
            return
        def natural(name):
            return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]
        paths = sorted((os.path.join(folder, name) for name in os.listdir(folder)
                        if os.path.splitext(name)[1].lstrip(".").lower() in VIDEO_EXTENSIONS),
                       key=lambda p: natural(os.path.basename(p)))
        if not paths:
            messagebox.showinfo(APP_NAME, "No videos found in that folder.")
            return
        batch_add(paths)

    def batch_remove():
        sel = batch_list.curselection()
        if sel:
            p = batch_files.pop(sel[0])
            batch_statuses.pop(p, None)
            refresh_batch()

    def batch_clear():
        batch_files.clear()
        batch_statuses.clear()
        refresh_batch()

    ttk.Button(batch_side, text="Add files...", command=batch_add).pack(fill="x")
    ttk.Button(batch_side, text="Add folder...", command=batch_add_folder).pack(fill="x", pady=4)
    ttk.Button(batch_side, text="Remove", command=batch_remove).pack(fill="x")
    ttk.Button(batch_side, text="Clear", command=batch_clear).pack(fill="x", pady=4)

    controls = ttk.Frame(t_batch)
    controls.pack(fill="x", pady=(0, 4))
    ttk.Label(controls, text="Action:").pack(side="left")
    batch_action = tk.StringVar(value="Convert")
    batch_action_box = ttk.Combobox(controls, textvariable=batch_action, values=["Convert", "Repair"],
                                    state="readonly", width=10)
    batch_action_box.pack(side="left", padx=6)
    batch_convert_controls = ttk.Frame(controls)
    batch_convert_controls.pack(side="left", padx=(8, 0))
    ttk.Label(batch_convert_controls, text="Make it:").pack(side="left")
    batch_fmt = tk.StringVar(value=labels[0])
    ttk.Combobox(batch_convert_controls, textvariable=batch_fmt, values=labels,
                 state="readonly", width=30).pack(side="left", padx=6)
    ttk.Label(batch_convert_controls, text="Quality:").pack(side="left", padx=(8, 0))
    batch_quality = tk.StringVar(value="Balanced")
    ttk.Combobox(batch_convert_controls, textvariable=batch_quality,
                 values=["Smaller", "Balanced", "High"], state="readonly", width=10).pack(side="left", padx=6)
    ttk.Label(batch_convert_controls, text="Size:").pack(side="left", padx=(8, 0))
    batch_height = tk.StringVar(value="Original")
    ttk.Combobox(batch_convert_controls, textvariable=batch_height,
                 values=["Original", "1080p", "720p", "480p"], state="readonly", width=10).pack(side="left", padx=6)
    batch_mute = tk.BooleanVar(value=False)
    ttk.Checkbutton(batch_convert_controls, text="Mute audio", variable=batch_mute).pack(side="left", padx=(8, 0))
    batch_deep_var = tk.BooleanVar(value=False)
    batch_deep = ttk.Checkbutton(controls, text="Deep repair", variable=batch_deep_var)

    def on_batch_action(*_):
        if batch_action.get() == "Convert":
            batch_convert_controls.pack(side="left", padx=(8, 0))
            batch_deep.pack_forget()
        else:
            batch_convert_controls.pack_forget()
            batch_deep.pack(side="left", padx=(8, 0))
    batch_action_box.bind("<<ComboboxSelected>>", on_batch_action)
    on_batch_action()

    def do_batch():
        if not batch_files:
            messagebox.showinfo(APP_NAME, "Add some videos first.")
            return
        files = list(batch_files)
        action = batch_action.get()
        quality_map = {"Smaller": "smaller", "Balanced": "balanced", "High": "high"}
        height_map = {"Original": None, "1080p": 1080, "720p": 720, "480p": 480}
        quality = quality_map[batch_quality.get()]
        height = height_map[batch_height.get()]
        mute = batch_mute.get()
        deep = batch_deep_var.get()
        batch_ext = re.search(r"\(\.(\w+)\)$", batch_fmt.get()).group(1)
        folder = outdir_var.get().strip() if use_outdir.get() else None
        for p in files:
            batch_statuses[p] = "ready"
        refresh_batch()

        def work(job):
            results, errors = [], []
            for i, src in enumerate(files):
                if job.cancelled:
                    break
                batch_statuses[src] = "working"
                events.put(("batch_status", (src, "working")))
                job.status(f"File {i + 1} of {len(files)}: {os.path.basename(src)}")
                try:
                    if action == "Convert":
                        dst = suggest_output(src, "converted", batch_ext, folder)
                        engine.convert(src, dst, job, quality=quality, scale_height=height, mute=mute)
                    else:
                        src_ext = _ext(src)
                        dst = suggest_output(src, "repaired",
                                             src_ext if src_ext in FORMATS else "mkv", folder)
                        engine.repair(src, dst, job, deep)
                    results.append(dst)
                    batch_statuses[src] = "done"
                except Cancelled:
                    raise
                except Exception as e:
                    errors.append(f"{os.path.basename(src)}: {e}")
                    batch_statuses[src] = "error"
                events.put(("batch_status", (src, batch_statuses[src])))
            if job.cancelled:
                raise Cancelled("Cancelled.")
            if errors:
                raise TeaCutError(f"{len(errors)} file(s) failed:\n" + "\n".join(errors[:5]))
            return results
        start(work, "Batch...")
    big_button(t_batch, "Start batch", do_batch)

    # -- drag & drop (only if tkinterdnd2 is installed) ---------------------
    if DND_FILES:
        def on_drop(event):
            paths = root.tk.splitlist(event.data)
            current = notebook.index(notebook.select())
            if current == 1:
                add_files(paths)
            elif current == 4 and paths:
                batch_add(paths)
            elif paths:
                (cut_set, None, rep_set, conv_set)[current](paths[0])
        root.drop_target_register(DND_FILES)
        root.dnd_bind("<<Drop>>", on_drop)

    # Files passed on the command line / dropped onto the program icon.
    initial = [a for a in sys.argv[1:] if os.path.isfile(a)]
    if len(initial) == 1:
        root.after(200, lambda: cut_set(initial[0]))
    elif len(initial) > 1:
        notebook.select(1)
        root.after(200, lambda: add_files(initial))

    def on_close():
        if state["job"]:
            if not messagebox.askyesno(APP_NAME, "A job is still running. Stop it and quit?"):
                return
            state["job"].cancel()
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)
    apply_theme()
    # Size the window to fit its contents: fonts and DPI scaling differ per PC,
    # so a fixed size can clip the bottom row.
    root.update_idletasks()
    width, height = max(660, root.winfo_reqwidth()), max(520, root.winfo_reqheight())
    root.geometry(f"{width}x{height}")
    root.minsize(min(width, 600), height)
    root.after(150, lambda: set_title_bar(dark_mode.get()))
    root.mainloop()


def main():
    # A windowed .exe has no console; give print() somewhere harmless to go.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    args = sys.argv[1:]
    if args and args[0] in ("info", "repair", "cut", "split", "join", "convert", "-h", "--help", "--version"):
        sys.exit(cli(args))
    gui()


if __name__ == "__main__":
    main()
