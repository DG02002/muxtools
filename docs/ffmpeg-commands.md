# FFmpeg commands

This reference lists the FFmpeg options and sample commands used to remux media into MP4 containers for QuickTime Player playback.

## Options

| Option | Description |
| --- | --- |
| `-c:v copy` | Copies the video stream without re-encoding. |
| `-tag:v <tag>` | Sets the FourCC tag for the video stream (such as `avc1`, `hvc1`, `dvh1`, or `dva1`). |
| `-c:a copy` | Copies audio streams without re-encoding. |
| `-c:a alac` | Transcodes audio to Apple Lossless Audio Codec (ALAC). |
| `-c:s mov_text` | Converts text subtitle streams to MPEG-4 Timed Text (`mov_text`). |
| `-sn` | Disables subtitle recording and excludes subtitle streams from the output. |
| `-dn` | Disables data stream recording and excludes data streams from the output. |
| `-map_chapters 0` | Copies chapters from the input file to the next output file. |
| `-map_metadata 0` | Copies global metadata from the input file to the next output file. |
| `-movflags +faststart` | Moves the index (`moov` atom) to the beginning of the file for faster playback start. |
| `-brand mp42` | Sets the major brand of the MP4 container to `mp42`. |
| `-strict unofficial` | Enables unofficial MP4 muxer extensions in FFmpeg. |

## Sample commands

### Standard remuxing (H.264, HEVC, AV1)

```bash
ffmpeg -i INPUT -map 0 -map_chapters 0 -map_metadata 0 -c:v copy -c:a copy -c:s mov_text -dn -movflags +faststart -brand mp42 -strict unofficial OUTPUT.mp4
```

### Dolby Vision Profile 5

```bash
ffmpeg -i INPUT -map 0 -map_chapters 0 -map_metadata 0 -c:v copy -c:a copy -c:s mov_text -dn -movflags +faststart -brand mp42 -tag:v dvh1 -strict unofficial OUTPUT.mp4
```

### Dolby Vision Profile 8 or HEVC HDR10

```bash
ffmpeg -i INPUT -map 0 -map_chapters 0 -map_metadata 0 -c:v copy -c:a copy -c:s mov_text -dn -movflags +faststart -brand mp42 -tag:v hvc1 -strict unofficial OUTPUT.mp4
```

### Dolby Vision Profile 9

```bash
ffmpeg -i INPUT -map 0 -map_chapters 0 -map_metadata 0 -c:v copy -c:a copy -c:s mov_text -dn -movflags +faststart -brand mp42 -tag:v dva1 -strict unofficial OUTPUT.mp4
```

### Unsupported audio conversion to Apple Lossless (ALAC)

```bash
ffmpeg -i INPUT -map 0 -map_chapters 0 -map_metadata 0 -c:v copy -c:a alac -c:s mov_text -dn -movflags +faststart -brand mp42 -strict unofficial OUTPUT.mp4
```

### Incompatible bitmap subtitles

```bash
ffmpeg -i INPUT -map 0 -map_chapters 0 -map_metadata 0 -c:v copy -c:a copy -sn -dn -movflags +faststart -brand mp42 -strict unofficial OUTPUT.mp4
```

## References

- [Codec compatibility](codec-compatibility.md): Media format and stream compatibility reference
- [FFmpeg documentation](https://ffmpeg.org/ffmpeg.html): Official command-line tool reference
