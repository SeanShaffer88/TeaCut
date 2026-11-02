"""Tests for TeaCut's helpers, engine and command line."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import teacut


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("90", 90.0),
    ("1:30", 90.0),
    ("01:02:03.5", 3723.5),
    ("1m30s", 90.0),
    ("1h2m3s", 3723.0),
    ("1.5", 1.5),
    ("0", 0.0),
    ("1h30", 3630.0),
])
def test_parse_time(text, expected):
    assert teacut.parse_time(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "abc", "-5", "1:2:3:4", "1:xx"])
def test_parse_time_rejects(text):
    with pytest.raises(ValueError):
        teacut.parse_time(text)


@pytest.mark.parametrize("secs,expected", [
    (0, "0:00"),
    (6.09, "0:06.09"),
    (6.5, "0:06.5"),
    (9.5, "0:09.5"),
    (59.999, "0:59.999"),
    (59.9999, "1:00"),          # must not print "0:60"
    (60, "1:00"),
    (90, "1:30"),
    (3599.9999, "1:00:00"),     # must not print "59:60"
    (3600, "1:00:00"),
    (3723.5, "1:02:03.5"),
])
def test_format_time(secs, expected):
    assert teacut.format_time(secs) == expected


def test_format_time_clamps_negative():
    assert teacut.format_time(-5) == "0:00"


def test_ext():
    assert teacut._ext("movie.MP4") == "mp4"
    assert teacut._ext("movie") == "mkv"
    assert teacut._ext("archive.tar.gz") == "gz"


def test_quote():
    assert teacut._quote("plain") == "plain"
    assert teacut._quote("with space") == '"with space"'


def test_unique_path(tmp_path):
    target = tmp_path / "movie_cut.mkv"
    target.write_text("x")
    second = teacut.unique_path(str(target))
    assert second == str(tmp_path / "movie_cut (2).mkv")
    Path(second).write_text("x")
    third = teacut.unique_path(str(target))
    assert third == str(tmp_path / "movie_cut (3).mkv")


def test_suggest_output(tmp_path):
    src = str(tmp_path / "movie.mkv")
    assert teacut.suggest_output(src, "cut") == str(tmp_path / "movie_cut.mkv")
    assert teacut.suggest_output(src, "converted", "mp4") == str(tmp_path / "movie_converted.mp4")


def test_suggest_output_in_outdir(tmp_path):
    src = str(tmp_path / "movie.mkv")
    outdir = tmp_path / "results"
    assert teacut.suggest_output(src, "cut", None, str(outdir)) == str(outdir / "movie_cut.mkv")
    assert teacut.suggest_output(src, "converted", "mp4", str(outdir)) == str(outdir / "movie_converted.mp4")


def test_resolve_output(tmp_path):
    src = str(tmp_path / "movie.mkv")
    outdir = tmp_path / "results"
    assert teacut.resolve_output(None, src, "cut", str(outdir)) == str(outdir / "movie_cut.mkv")
    assert teacut.resolve_output("clip.mp4", src, "cut", str(outdir)) == str(outdir / "clip.mp4")
    explicit = str(tmp_path / "explicit.mp4")
    assert teacut.resolve_output(explicit, src, "cut", str(outdir)) == explicit


def test_ensure_parent(tmp_path):
    target = tmp_path / "a" / "b" / "c" / "out.mkv"
    teacut.ensure_parent(str(target))
    assert target.parent.is_dir()


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    settings = {"dark": True, "outdir": str(tmp_path / "results")}
    teacut.save_settings(settings)
    assert teacut.load_settings() == settings


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def test_probe(engine, wmv):
    info = engine.probe(wmv)
    assert info.has_video and info.has_audio
    assert info.vcodec == "wmv2"
    assert info.acodec == "wmav2"
    assert (info.width, info.height) == (320, 240)
    assert info.fps == pytest.approx(25)
    assert 5.5 < info.duration < 6.5


def test_probe_missing_file(engine, tmp_path):
    with pytest.raises(teacut.TeaCutError):
        engine.probe(str(tmp_path / "does-not-exist.wmv"))


def test_probe_text_file(engine, tmp_path):
    path = tmp_path / "hello.txt"
    path.write_text("this is not a video")
    with pytest.raises(teacut.TeaCutError):
        engine.probe(str(path))


# ---------------------------------------------------------------------------
# Cut
# ---------------------------------------------------------------------------

def test_cut_lossless(engine, wmv, tmp_path, job):
    dst = str(tmp_path / "cut.wmv")
    engine.cut(wmv, dst, 1, 4, job)
    assert os.path.getsize(dst) > 1024
    info = engine.probe(dst)
    assert 1.0 < info.duration < 5.5


def test_cut_exact(engine, wmv, tmp_path, job):
    dst = str(tmp_path / "exact.mp4")
    engine.cut(wmv, dst, 1, 2, job, exact=True)
    assert os.path.getsize(dst) > 1024
    info = engine.probe(dst)
    assert 0.8 < info.duration < 1.5


def test_cut_remove(engine, wmv, tmp_path, job):
    dst = str(tmp_path / "removed.wmv")
    engine.cut(wmv, dst, 2, 4, job, remove=True)
    assert os.path.getsize(dst) > 1024
    info = engine.probe(dst)
    assert 3.5 < info.duration < 5.0


def test_cut_clamps_end_to_duration(engine, wmv, tmp_path, job):
    dst = str(tmp_path / "tail.wmv")
    engine.cut(wmv, dst, 5, 100, job)
    info = engine.probe(dst)
    assert 0.5 < info.duration < 2.0


def test_cut_rejects_bad_range(engine, wmv, tmp_path, job):
    with pytest.raises(teacut.TeaCutError):
        engine.cut(wmv, str(tmp_path / "bad.wmv"), 5, 4, job)


def test_cut_remove_everything(engine, wmv, tmp_path, job):
    with pytest.raises(teacut.TeaCutError):
        engine.cut(wmv, str(tmp_path / "bad.wmv"), 0, 100, job, remove=True)


def test_cut_creates_output_folder(engine, wmv, tmp_path, job):
    dst = str(tmp_path / "a" / "b" / "out.wmv")
    engine.cut(wmv, dst, 1, 2, job)
    assert os.path.isfile(dst)


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------

def test_join_lossless(engine, wmv, wmv2, tmp_path, job):
    dst = str(tmp_path / "joined.wmv")
    engine.join([wmv, wmv2], dst, job)
    assert os.path.getsize(dst) > 1024
    info = engine.probe(dst)
    assert 8.0 < info.duration < 10.0


def test_join_mismatched(engine, wmv, avi, tmp_path, job):
    dst = str(tmp_path / "joined.mp4")
    engine.join([wmv, avi], dst, job)
    assert os.path.getsize(dst) > 1024
    info = engine.probe(dst)
    assert 9.0 < info.duration < 11.0


def test_join_needs_two_files(engine, wmv, tmp_path, job):
    with pytest.raises(teacut.TeaCutError):
        engine.join([wmv], str(tmp_path / "one.wmv"), job)


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------

def test_repair(engine, wmv, tmp_path, job):
    dst = str(tmp_path / "fixed.wmv")
    engine.repair(wmv, dst, job)
    assert os.path.getsize(dst) > 1024
    engine.probe(dst)


def test_repair_deep(engine, wmv, tmp_path, job):
    dst = str(tmp_path / "deep.wmv")
    engine.repair(wmv, dst, job, deep=True)
    assert os.path.getsize(dst) > 1024
    engine.probe(dst)


def test_repair_truncated(engine, wmv, tmp_path, job):
    broken = tmp_path / "broken.wmv"
    broken.write_bytes(Path(wmv).read_bytes()[:200000])
    dst = str(tmp_path / "broken_fixed.wmv")
    engine.repair(str(broken), dst, job)
    assert os.path.getsize(dst) > 1024
    engine.probe(dst)


# ---------------------------------------------------------------------------
# Convert
# ---------------------------------------------------------------------------

def test_convert_lossless(engine, wmv, tmp_path, job):
    dst = str(tmp_path / "out.mkv")
    engine.convert(wmv, dst, job)
    info = engine.probe(dst)
    assert info.vcodec == "wmv2"


def test_convert_same_format(engine, wmv, tmp_path, job):
    dst = str(tmp_path / "out.wmv")
    engine.convert(wmv, dst, job)
    assert os.path.getsize(dst) > 1024
    engine.probe(dst)


def test_convert_reencode(engine, wmv, tmp_path, job):
    dst = str(tmp_path / "out.mp4")
    engine.convert(wmv, dst, job)
    info = engine.probe(dst)
    assert info.vcodec in ("h264", "mpeg4")
    assert info.acodec == "aac"


def test_convert_quality_and_mute(engine, wmv, tmp_path, job):
    dst = str(tmp_path / "small.mp4")
    engine.convert(wmv, dst, job, quality="smaller", scale_height=240, mute=True)
    info = engine.probe(dst)
    assert info.has_video
    assert not info.has_audio
    assert info.height <= 240


def test_convert_scale_down(engine, avi, tmp_path, job):
    dst = str(tmp_path / "down.mp4")
    engine.convert(avi, dst, job, scale_height=240)
    info = engine.probe(dst)
    assert info.height == 240
    assert info.width == 426


def test_convert_never_upscales(engine, wmv, tmp_path, job):
    dst = str(tmp_path / "up.mp4")
    engine.convert(wmv, dst, job, scale_height=1080)
    info = engine.probe(dst)
    assert info.height == 240


def test_convert_keeps_subtitles_in_mkv(engine, sub_mkv, tmp_path, job):
    dst = str(tmp_path / "sub.mkv")
    engine.convert(sub_mkv, dst, job)
    info = engine.probe(dst)
    assert info.has_subtitles


def test_convert_keeps_subtitles_when_reencoding(engine, sub_mkv, tmp_path, job):
    dst = str(tmp_path / "sub_reencoded.mkv")
    engine.convert(sub_mkv, dst, job, force_encode=True)
    info = engine.probe(dst)
    assert info.has_subtitles


def test_split(engine, wmv, tmp_path, job):
    outdir = tmp_path / "parts"
    paths = engine.split(wmv, str(outdir), 3, job)
    assert len(paths) == 3
    for path in paths:
        assert os.path.isfile(path)
        assert os.path.getsize(path) > 1024
        info = engine.probe(path)
        assert 0.5 < info.duration < 4.0


def test_split_exact(engine, wmv, tmp_path, job):
    outdir = tmp_path / "parts_exact"
    paths = engine.split(wmv, str(outdir), 2, job, exact=True)
    assert len(paths) == 2
    for path in paths:
        assert os.path.isfile(path)
        info = engine.probe(path)
        assert 2.5 < info.duration < 3.5


def test_split_rejects_bad_parts(engine, wmv, tmp_path, job):
    with pytest.raises(teacut.TeaCutError):
        engine.split(wmv, str(tmp_path), 1, job)
    with pytest.raises(teacut.TeaCutError):
        engine.split(wmv, str(tmp_path), 100, job)


def test_extract_frame(ffmpeg, wmv, tmp_path):
    dst = str(tmp_path / "frame.png")
    assert teacut.extract_frame(ffmpeg, wmv, dst, 1.0) == dst
    assert os.path.getsize(dst) > 100


def test_extract_frame_no_video(ffmpeg, tmp_path):
    path = tmp_path / "silent.mka"
    path.write_bytes(b"not a video")
    assert teacut.extract_frame(ffmpeg, str(path), str(tmp_path / "no.png"), 0.0) is None


@pytest.mark.parametrize("ext", sorted(teacut.FORMATS))
def test_convert_every_format(engine, short_wmv, tmp_path, job, ext, ts_supported):
    if ext == "ts" and not ts_supported:
        pytest.skip("this FFmpeg build crashes when reading MPEG-TS")
    dst = str(tmp_path / f"out.{ext}")
    engine.convert(short_wmv, dst, job)
    assert os.path.getsize(dst) > 1024
    engine.probe(dst)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def run_cli(*args):
    return subprocess.run(
        [sys.executable, teacut.__file__, *args],
        check=False, capture_output=True, text=True, timeout=180)


def test_cli_version():
    result = run_cli("--version")
    assert result.returncode == 0
    assert teacut.VERSION in result.stdout


def test_cli_info(wmv):
    result = run_cli("info", wmv)
    assert result.returncode == 0, result.stderr
    assert "container" in result.stdout


def test_cli_cut(wmv, tmp_path):
    out = str(tmp_path / "cli_cut.wmv")
    result = run_cli("cut", wmv, "--start", "1", "--end", "4", "-o", out)
    assert result.returncode == 0, result.stderr
    assert os.path.isfile(out)


def test_cli_cut_output_dir(wmv, tmp_path):
    outdir = tmp_path / "results"
    result = run_cli("cut", wmv, "--start", "1", "--end", "4", "--output-dir", str(outdir))
    assert result.returncode == 0, result.stderr
    assert (outdir / "sample_cut.wmv").is_file()


def test_cli_join(wmv, wmv2, tmp_path):
    out = str(tmp_path / "cli_join.wmv")
    result = run_cli("join", wmv, wmv2, "-o", out)
    assert result.returncode == 0, result.stderr
    assert os.path.isfile(out)


def test_cli_repair(wmv, tmp_path):
    out = str(tmp_path / "cli_repair.wmv")
    result = run_cli("repair", wmv, "-o", out)
    assert result.returncode == 0, result.stderr
    assert os.path.isfile(out)


def test_cli_repair_output_dir(wmv, tmp_path):
    outdir = tmp_path / "results"
    result = run_cli("repair", wmv, "--output-dir", str(outdir))
    assert result.returncode == 0, result.stderr
    assert (outdir / "sample_repaired.wmv").is_file()


def test_cli_join_output_dir(wmv, wmv2, tmp_path):
    outdir = tmp_path / "results"
    result = run_cli("join", wmv, wmv2, "--output-dir", str(outdir))
    assert result.returncode == 0, result.stderr
    assert (outdir / "sample_joined.wmv").is_file()


def test_cli_convert(wmv, tmp_path):
    out = str(tmp_path / "cli_convert.mp4")
    result = run_cli("convert", wmv, "-o", out)
    assert result.returncode == 0, result.stderr
    assert os.path.isfile(out)


def test_cli_convert_output_dir(wmv, tmp_path):
    outdir = tmp_path / "results"
    result = run_cli("convert", wmv, "-o", "cli_convert.mp4", "--output-dir", str(outdir))
    assert result.returncode == 0, result.stderr
    assert (outdir / "cli_convert.mp4").is_file()


def test_cli_convert_presets(wmv, tmp_path):
    out = str(tmp_path / "cli_small.mp4")
    result = run_cli("convert", wmv, "-o", out, "--quality", "smaller",
                     "--scale", "240", "--mute")
    assert result.returncode == 0, result.stderr
    assert os.path.isfile(out)
    info = teacut.Engine().probe(out)
    assert info.height <= 240
    assert not info.has_audio


def test_cli_split(wmv, tmp_path):
    outdir = tmp_path / "parts"
    result = run_cli("split", wmv, "--parts", "3", "--output-dir", str(outdir))
    assert result.returncode == 0, result.stderr
    assert len(list(outdir.glob("sample_part*.wmv"))) == 3


def test_cli_bad_time(wmv, tmp_path):
    result = run_cli("cut", wmv, "--start", "5", "--end", "4", "-o", str(tmp_path / "x.wmv"))
    assert result.returncode == 1
    assert "Error" in result.stderr


def test_cli_join_one(wmv, tmp_path):
    result = run_cli("join", wmv, "-o", str(tmp_path / "x.wmv"))
    assert result.returncode == 1


def test_cli_missing_file(tmp_path):
    result = run_cli("info", str(tmp_path / "nope.wmv"))
    assert result.returncode == 1
