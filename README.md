# muxtools

Media remuxing and audio processing workflows for macOS and QuickTime Player compatibility.

## Scripts

| Script | Description |
| :--- | :--- |
| [`scripts/quickmp4-gpac.py`](scripts/quickmp4-gpac.py) | Remuxes MKV, WebM, and other video files into QuickTime-compatible MP4s using GPAC. Reframes video bitstreams, sets FourCC tags (`avc1`, `hvc1`, `av01`, `dvh1`, `dva1`), transcodes unsupported audio to ALAC, and converts text subtitles to Timed Text (`tx3g`). |
| [`scripts/remuxddp.py`](scripts/remuxddp.py) | Pipeline that downloads media archives, converts audio streams to Dolby Digital Plus (E-AC-3), repackages archives, and uploads the result. |

## Requirements

- Python 3
- Core media tools:

  ```bash
  brew install gpac
  ```

  Installing `gpac` automatically installs `ffmpeg` and `ffprobe`.

- Remote downloads (`scripts/remuxddp.py`):

  ```bash
  brew install aria2
  ```

## Usage

### [`scripts/quickmp4-gpac.py`](scripts/quickmp4-gpac.py)

```bash
$ python3 scripts/quickmp4-gpac.py --help
usage: quickmp4-gpac.py [-h] [-o OUTPUT] [-v] sources [sources ...]

Remux media into QuickTime-compatible MP4 files using GPAC

positional arguments:
  sources               Media files or folders to process.

optional arguments:
  -h, --help            show this help message and exit
  -o OUTPUT, --output OUTPUT
                        Optional output file or folder.
  -v, --verbose         Show full GPAC log output and warnings.
```

### [`scripts/remuxddp.py`](scripts/remuxddp.py)

```bash
$ python3 scripts/remuxddp.py --help
usage: remuxddp.py [-h] source

Acquire media, remux audio to Dolby Digital Plus (E-AC-3), and upload

positional arguments:
  source      URL, archive, or media file to process.

optional arguments:
  -h, --help  show this help message and exit
```

## Technical details

### QuickTime and AVFoundation remuxing (`quickmp4-gpac.py`)

QuickTime Player refuses to open MKV containers and fails on common formats like DTS audio or PGS subtitles. Standard FFmpeg remuxing often produces unplayable or desynced MP4s on macOS because its MP4 muxer mishandles out-of-band parameter sets and multi-track subtitles.

`quickmp4-gpac.py` uses GPAC's filter pipeline to build clean MP4 containers:

- **NAL reframing.** Extracts parameter sets out-of-band into standard `avcC` and `hvcC` sample description boxes, strips in-band AUDs, and writes indexed sync tables (`stss`).
- **Dolby Vision sample entries.** Writes `dvh1` for profile 5 and `dva1` for profile 9. Profile 7 and 8 fall back to standard `hvc1` to maintain HDR10 base-layer compatibility.
- **Audio conversion.** Audio codecs supported natively by QuickTime (AAC, ALAC, AC-3, E-AC-3, MP3) pass through untouched. Incompatible formats (FLAC, DTS, TrueHD, Opus) transcode to Apple Lossless (ALAC) without touching the video stream.
- **Subtitle muxing.** Converts text subtitles (SRT, ASS, WebVTT) into standard QuickTime Timed Text (`tx3g`) across multiple tracks, and drops unsupported bitmap subtitles (PGS, VobSub).

### Batch audio transcoding and packaging (`scripts/remuxddp.py`)

- **Standardized surround audio.** Converts multi-channel surround tracks (FLAC, TrueHD, DTS) into Dolby Digital Plus (E-AC-3) at 640 kbit/s for 5.1 and 224 kbit/s for stereo, normalized to -31 dBFS dialnorm.
- **End-to-end automation.** Downloads remote archives with `aria2c` (falling back to `curl`), unpacks contents, filters audio streams by language (defaulting to English and Japanese), repackages outputs into tar archives, and uploads the result.

## Samples

- [Sample metadata](ffprobe-sample-metadata/): Sample JSON metadata from ffprobe across video formats and HDR profiles
