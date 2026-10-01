# muxtools

Media remuxing and audio processing workflows for macOS and QuickTime Player compatibility.

## Scripts

| Script | Description |
| :--- | :--- |
| [`scripts/quickmp4.py`](scripts/quickmp4.py) | Remuxes MKV, WebM, and other video files into QuickTime-compatible MP4s using GPAC. |
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

## Recommended usage

Run directly via `curl` to execute the latest version without cloning the repository or pulling updates manually:

```bash
curl -fsSL https://raw.githubusercontent.com/DG02002/muxtools/main/scripts/quickmp4.py | python3 - input.mkv
```

To add `quickmp4` as a command in your zsh profile (`~/.zprofile`):

```bash
echo 'quickmp4() { curl -fsSL https://raw.githubusercontent.com/DG02002/muxtools/main/scripts/quickmp4.py | python3 - "$@"; }' >> ~/.zprofile && source ~/.zprofile
```

## Usage

### [`scripts/quickmp4.py`](scripts/quickmp4.py)

Remux a single file (writes `movie.quickmp4.mp4` next to the source):

```bash
quickmp4 movie.mkv
```

Remux all media files in a folder:

```bash
quickmp4 /path/to/season_folder/
```

Specify a custom output file or directory:

```bash
quickmp4 movie.mkv -o /path/to/output/
```

Enable verbose GPAC logging:

```bash
quickmp4 movie.mkv -v
```

### [`scripts/remuxddp.py`](scripts/remuxddp.py)

Download, convert audio to E-AC-3, repackage, and upload:

```bash
python3 scripts/remuxddp.py "https://example.com/media.zip"
```

Process a local archive or media file directly:

```bash
python3 scripts/remuxddp.py /path/to/archive.tar.gz
```

Interrupted downloads are resumable: in-progress files live under `~/Downloads/remuxddp-partial/<url-hash>/` and survive failed or canceled runs, so re-running the script with the same URL picks up where the download left off. The folder is cleaned up automatically after a successful upload.

## Documentation

- [QuickTime Player compatibility](quicktime-player-compatibility.md): Format compatibility table covering containers, video, audio, subtitles, and metadata

## Samples

- [Sample metadata](ffprobe-sample-metadata/): Sample JSON metadata from ffprobe across video formats and HDR profiles
