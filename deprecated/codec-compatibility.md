# Codec compatibility

Reference for how muxtools handles MP4 container tags, QuickTime video playback, audio transcoding, and subtitle streams.

## Video playback

The following table lists video format support in QuickTime Player.

| Format | Tag | Playback |
| --- | ---: | :---: |
| H.264 / AVC SDR | `avc1` | Yes |
| H.264 Dolby Vision Profile 9 | `dva1` | Conditional |
| HEVC SDR | `hvc1` | Yes |
| HEVC HDR10 | `hvc1` | Yes |
| HEVC HLG | `hvc1` | Conditional |
| HEVC HDR10+ | `hvc1` | Conditional |
| HEVC Dolby Vision Profile 5 | `dvh1` | Yes |
| HEVC Dolby Vision Profile 8 | `hvc1` | Conditional |
| AV1 | `av01` | Conditional |
| VC-1 | `vc-1` | No |

QuickTime Player may display only the HDR10 base layer of HDR10+ video. Support for HLG, Dolby Vision profile 8, and AV1 depends on your macOS version, hardware decoding support, and display capabilities.

## MP4 muxer compatibility

FFmpeg requires `-strict unofficial` to write certain MP4 sample entries. Without this flag, FFmpeg refuses to write Dolby Vision metadata boxes. Keep this flag enabled unless target devices fail to parse unofficial container extensions.

GPAC natively handles these sample descriptions via the `rfnalu` filter and `mp4mx` muxer.

## Audio handling

Audio formats natively supported by QuickTime are copied directly. Incompatible decodable streams are converted to Apple Lossless (ALAC).

| Codec | Tag | Action |
| --- | --- | --- |
| AAC | `mp4a` | Copy |
| ALAC | `alac` | Copy |
| AC-3 / Dolby Digital | `ac-3` | Copy |
| E-AC-3 / Dolby Digital Plus | `ec-3` | Copy |
| MP3 | `mp4a` | Copy |
| FLAC | `fLaC` | Convert to ALAC |
| Opus | `Opus` | Convert to ALAC |
| Vorbis | - | Convert to ALAC |
| DTS / DTS-HD | `dtsc`, `dtse`, `dtsh`, `dtsl` | Convert to ALAC |
| Dolby TrueHD / MLP | `mlpa` | Convert to ALAC |
| MPEG Layer I or II | `mp4a` | Convert to ALAC |
| MPEG-4 ALS / SLS | `mp4a` | Convert to ALAC |
| LPCM and other PCM variants | Varies by PCM representation | Convert to ALAC |
| AMR-NB / AMR-WB | `samr`, `sawb` | Convert to ALAC |
| Any other decodable audio | - | Convert to ALAC |

Converting lossy audio to ALAC does not recover lost quality, but it avoids a second lossy encode while ensuring QuickTime compatibility.

## Subtitle, chapter, and metadata handling

Text subtitles that fit the MP4 container are converted to QuickTime Timed Text. Bitmap subtitles and unsupported data streams are dropped.

| Source | Action |
| --- | --- |
| MP4 Timed Text (`mov_text` / `tx3g`) | Copy |
| SRT / SubRip | Convert to `mov_text` / `tx3g` |
| ASS / SSA | Convert to `mov_text` / `tx3g` |
| WebVTT and plain text | Convert to `mov_text` / `tx3g` |
| PGS, VobSub, DVD, DVB, and XSUB | Drop |
| Chapters | Copy |
| Global metadata | Copy |
| Data streams and attachments | Drop |

## References

- [FFmpeg commands](ffmpeg-commands.md): Reference for remuxing commands across stream types
- [MP4 Registration Authority](https://mp4ra.org/): Registration authority for code points in the ISO Base Media File Format
- [Apple: High Dynamic Range Metadata for Apple Devices](https://developer.apple.com/av-foundation/High-Dynamic-Range-Metadata-for-Apple-Devices.pdf)
- [Apple: Incorporating HDR video with Dolby Vision into your apps](https://developer.apple.com/av-foundation/Incorporating-HDR-video-with-Dolby-Vision-into-your-apps.pdf)
- [Apple: QuickTime Player supported media formats](https://support.apple.com/guide/quicktime-player/open-and-play-a-file-qtp6cee0761b/mac)
