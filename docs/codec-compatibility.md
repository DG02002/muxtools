# Codec compatibility

This reference explains how quickmp4 handles MP4 tags, QuickTime video playback, and media streams.

## Video playback

The following table lists the video formats quickmp4 handles. Playback status applies to QuickTime Player.

| Format | Tag | Playback |
| --- | ---: | :---: |
| H.264 / AVC SDR | `avc1` | Yes |
| H.264 Dolby Vision Profile 9 | `dva1` | Conditional |
| HEVC SDR | `hvc1` | Yes |
| HEVC HDR10 | `hvc1` | Yes |
| HEVC HLG | `hvc1` | Conditional |
| HEVC HDR10+ | `hvc1` | Conditional |
| HEVC Dolby Vision Profile 5 | `dvh1` | Yes |
| HEVC Dolby Vision Profile 8 | `dvh1` | Conditional |
| AV1 | `av01` | Conditional |
| VC-1 | `vc-1` | No |

QuickTime Player might show only the HDR10 base layer of HDR10+ video. HLG, Dolby Vision Profile 8, and AV1 support depends on the source, macOS version, Mac hardware, and display. The current quickmp4 CLI writes `hvc1` for Dolby Vision Profile 8; `dvh1` output still needs fixture testing.

## MP4 muxer compatibility mode

quickmp4 passes `-strict unofficial` for every MP4 output. This option enables FFmpeg's unofficial MP4 extensions and supports quickmp4's tested compatibility profile. In local tests, QuickTime Player couldn't play Dolby Vision files produced without this option. Keep the option unless Dolby Vision playback succeeds on the target Apple hardware and software.

## Audio handling

quickmp4 copies the audio formats chosen for QuickTime compatibility and converts other decodable audio to Apple Lossless (ALAC).

| Codec | Tag | Action |
| --- | --- | --- |
| AAC | `mp4a` | Copy |
| ALAC | `alac` | Copy |
| AC-3 / Dolby Digital | `ac-3` | Copy |
| E-AC-3 / Dolby Digital Plus | `ec-3` | Copy |
| MP3 | `mp4a` | Copy |
| FLAC | `fLaC` | Convert to ALAC |
| Opus | `Opus` | Convert to ALAC |
| Vorbis | — | Convert to ALAC |
| DTS / DTS-HD | `dtsc`, `dtse`, `dtsh`, `dtsl` | Convert to ALAC |
| Dolby TrueHD / MLP | `mlpa` | Convert to ALAC |
| MPEG Layer I or II | `mp4a` | Convert to ALAC |
| MPEG-4 ALS / SLS | `mp4a` | Convert to ALAC |
| LPCM and other PCM variants | Varies by PCM representation | Convert to ALAC |
| AMR-NB / AMR-WB | `samr`, `sawb` | Convert to ALAC |
| Any other audio FFmpeg can decode | — | Convert to ALAC |

Converting lossy audio to ALAC can't restore lost quality. The conversion avoids another lossy encode but may increase the output size.

## Subtitle, chapter, and metadata handling

quickmp4 keeps text-based content that FFmpeg can represent in MP4 and omits unsupported bitmap subtitles and data streams.

| Source | Action |
| --- | --- |
| MP4 Timed Text (`mov_text` / `tx3g`) | Copy |
| SRT / SubRip | Convert to `mov_text` |
| ASS / SSA | Convert to `mov_text` |
| WebVTT and plain text | Convert to `mov_text` |
| PGS, VobSub, DVD, DVB, and XSUB | Omit |
| Chapters | Copy |
| Global metadata | Copy |
| Data streams and attachments | Omit |

## References

- [quickmp4 FFmpeg Commands](quickmp4-ffmpeg-commands.md): Quick reference of remuxing commands for various stream types
- [MP4 Registration Authority](https://mp4ra.org/): Official registration authority for code points in the ISO Base Media File Format family
- [Wikipedia: MP4 file format](https://en.wikipedia.org/wiki/MP4_file_format)
- [Wikipedia: Comparison of video container formats](https://en.wikipedia.org/wiki/Comparison_of_video_container_formats)
- [Wikipedia: List of codecs](https://en.wikipedia.org/wiki/List_of_codecs)
- [Apple: High Dynamic Range Metadata for Apple Devices](https://developer.apple.com/av-foundation/High-Dynamic-Range-Metadata-for-Apple-Devices.pdf)
- [Apple: Incorporating HDR video with Dolby Vision into your apps](https://developer.apple.com/av-foundation/Incorporating-HDR-video-with-Dolby-Vision-into-your-apps.pdf)
- [Apple: QuickTime Player supported media formats](https://support.apple.com/guide/quicktime-player/open-and-play-a-file-qtp6cee0761b/mac)

Use the MP4 Registration Authority to verify exact tags and capitalization. Registration confirms that a tag exists but doesn't confirm support in FFmpeg or QuickTime Player.
