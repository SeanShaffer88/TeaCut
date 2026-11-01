"""Shared fixtures for the TeaCut test suite.

The tests generate their own tiny videos with FFmpeg, so they don't depend on
any sample files being checked in.
"""

import os
import subprocess
import sys

import pytest

# Make ``import teacut`` work when pytest is run from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import teacut


def make_media(ffmpeg, path, vcodec, acodec, size, duration, fps=25, audio=True):
    """Write a short test clip with the given codecs."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc=s={size}:d={duration}:r={fps}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=d={duration}:r=44100"]
    cmd += ["-c:v", vcodec]
    if audio:
        cmd += ["-c:a", acodec]
    else:
        cmd += ["-an"]
    cmd += ["-shortest", str(path), "-y"]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


@pytest.fixture(scope="session")
def ffmpeg():
    exe = teacut.find_ffmpeg()
    if not exe:
        pytest.skip("FFmpeg not found (install imageio-ffmpeg or put ffmpeg on PATH)")
    return exe


@pytest.fixture(scope="session")
def engine(ffmpeg):
    return teacut.Engine(ffmpeg)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("teacut-media")


@pytest.fixture(scope="session")
def wmv(ffmpeg, media_dir):
    """A 6 second 320x240 WMV with sound."""
    path = media_dir / "sample.wmv"
    make_media(ffmpeg, path, "wmv2", "wmav2", "320x240", 6)
    return str(path)


@pytest.fixture(scope="session")
def short_wmv(ffmpeg, media_dir):
    """A 2 second WMV, for the slower format conversions."""
    path = media_dir / "short.wmv"
    make_media(ffmpeg, path, "wmv2", "wmav2", "320x240", 2)
    return str(path)


@pytest.fixture(scope="session")
def wmv2(ffmpeg, media_dir):
    """A second WMV with the same signature as ``wmv`` (so a join is lossless)."""
    path = media_dir / "same.wmv"
    make_media(ffmpeg, path, "wmv2", "wmav2", "320x240", 3)
    return str(path)


@pytest.fixture(scope="session")
def avi(ffmpeg, media_dir):
    """A 4 second AVI with a different size and codecs."""
    path = media_dir / "sample.avi"
    make_media(ffmpeg, path, "mpeg4", "mp2", "640x360", 4)
    return str(path)


@pytest.fixture(scope="session")
def ts_supported(engine, ffmpeg, media_dir):
    """Some static FFmpeg builds crash on MPEG-TS; skip that check if so."""
    path = media_dir / "ts-check.ts"
    try:
        make_media(ffmpeg, path, "libx264", "aac", "160x120", 1)
        engine.probe(str(path))
    except (teacut.TeaCutError, subprocess.CalledProcessError):
        return False
    return True


@pytest.fixture
def job():
    return teacut.Job()
