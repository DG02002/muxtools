# muxtools

Media remuxing and audio processing workflows for macOS and QuickTime compatibility.

## Why These Scripts Exist

Playing modern media files (MKV, WebM, AVI) natively in QuickTime Player or Apple's AVFoundation ecosystem without re-encoding video requires exact container, FourCC, audio codec, and subtitle configurations. Constructing these FFmpeg commands manually is repetitive and error-prone:

- **QuickTime Container & Tagging Quirks**: HEVC in MP4 requires the `hvc1` or `dvh1` FourCC tag instead of `hev1`. Dolby Vision requires `-strict unofficial` and specific profile tags.
- **Audio Codec Mismatches**: Formats like FLAC, DTS, TrueHD, and Opus fail to play natively in QuickTime. Converting them to Apple Lossless (`alac`) preserves 100% audio fidelity without re-encoding the video stream.
- **Subtitle Incompatibilities**: MP4 containers only support MPEG-4 Timed Text (`mov_text`). Text subtitles (SRT, ASS, WebVTT) must be converted, while unsupported bitmap subtitles (PGS, VobSub) must be stripped to prevent muxer errors.
- **Batch Pipeline Automation**: Handling batch media archives, converting surround audio to standardized Dolby Digital Plus (E-AC-3), repackaging, and uploading requires coordinating multiple tools (`ffmpeg`, `ffprobe`, `aria2c`, `curl`).

These scripts automate the stream inspection, tag resolution, transcoding, and packaging logic into simple commands.

## Scripts

| Script | Description |
| :--- | :--- |
| [`quickmp4.py`](quickmp4.py) | Remuxes MKV, WebM, and other video formats into QuickTime-compatible MP4s with proper FourCC tags (`avc1`, `hvc1`, `dvh1`), lossless audio fallback (ALAC), and `mov_text` subtitles. |
| [`remuxddp.py`](remuxddp.py) | Batch media pipeline that downloads, extracts archives, transcodes audio to Dolby Digital Plus (E-AC-3), repackages, and uploads. |

## Requirements

- **macOS** with `python3` (built-in)
- **FFmpeg** (`ffmpeg` and `ffprobe`): `brew install ffmpeg`
- **aria2** (`aria2c`, optional for remote downloads in `remuxddp`): `brew install aria2`

## Quick Usage

```bash
# Remux a video for QuickTime Player playback
python3 quickmp4.py video.mkv

# Batch remux all media in a folder
python3 quickmp4.py ~/Downloads/TVShow/

# Convert media audio to Dolby Digital Plus (E-AC-3) and upload
python3 remuxddp.py video.mkv
python3 remuxddp.py "https://example.com/archive.zip"
```

## Documentation

- [quickmp4 FFmpeg Commands](docs/quickmp4-ffmpeg-commands.md): Reference for underlying FFmpeg remuxing commands
- [Codec Compatibility](docs/codec-compatibility.md): QuickTime support matrix for video, audio, and subtitle streams
- [ffprobe Reference Captures](docs/ffprobe-captures/): JSON captures of various video formats and Dolby Vision streams
