# Steeper Revival

A remake of the old **Steeper** tool. The original was a small Windows app that
repaired, cut and joined ASF/WMV files using ASFCut. This version keeps that
spirit (one small window, a few buttons, instant lossless edits), but because
it runs on FFmpeg it opens **almost any video file**:

MP4 · MKV · AVI · MOV · WMV · ASF · FLV · WebM · MPEG/MPG · VOB · TS · M2TS/MTS ·
3GP · OGV · RM/RMVB · DV · MXF · DivX/Xvid · raw H.264/H.265 · and many more.

## What it does

| Tab | What it's for |
|-----|---------------|
| **Cut** | Keep a section, or remove a section and keep the rest. Instant and lossless by default; tick *Frame-exact* to cut on the exact frame. |
| **Join** | Put several videos end to end. Matching files are joined losslessly in seconds. Mismatched files (different sizes, formats, or some without sound) are automatically converted to match. |
| **Repair** | Fixes videos that won't open, can't seek, show the wrong length or stop early. The quick fix rebuilds the file losslessly; *Deep repair* re-encodes and rebuilds every timestamp for badly damaged files. |
| **Convert** | Change to MP4, MKV, WebM, MOV, AVI, WMV, ASF, MPEG-2, TS, FLV, M4V, 3GP or OGV. Any stream the new format already supports is copied untouched; only the rest is converted. |

Results are saved next to the original (e.g. `holiday_cut.mp4`). Nothing is
ever overwritten.

## Starting it

1. Install [Python 3](https://www.python.org/downloads/) (on Windows, tick *"Add python.exe to PATH"*).
2. Double-click **`Steeper (Windows).bat`**, or **`steeper-mac-linux.command`** on Mac/Linux.

The first launch downloads a copy of FFmpeg automatically (via `imageio-ffmpeg`).
If you already have FFmpeg installed, or put `ffmpeg.exe` next to `steeper.py`, that copy is used instead.
On Linux you may need the Tk package (`sudo apt install python3-tk`).

To make a standalone `.exe`, see `build-exe.txt`.

## Command line (optional)

```
python steeper.py cut video.wmv --start 1:30 --end 2:45 [--remove] [--exact]
python steeper.py join part1.mp4 part2.mp4 part3.avi -o all.mp4
python steeper.py repair broken.asf [--deep]
python steeper.py convert old.wmv -o new.mp4
python steeper.py info video.mkv
```

## Limits

* Lossless cuts snap to the nearest keyframe, so the start can be off by a second or so. Tick *Frame-exact* if that matters.
* An MP4/MOV whose recording was cut off (camera lost power, "moov atom not found") is missing its index entirely. Rebuilding that needs a tool such as `untrunc` and a healthy reference file from the same camera.
