"""Acquire media, remux audio to Dolby Digital Plus (E-AC-3), and upload.

Downloads remote files or archives using aria2c (with curl fallback), extracts
ZIP and TAR archives, remuxes audio streams to Dolby Digital Plus, repackages
archives, and uploads the output to bzzhr.co using curl.

Pipeline Workflow:
    - Download: Accelerated, resumable transfer via aria2c (fallback to curl)
    - Archive Extraction: Unpacks ZIP (.zip) and TAR (.tar, .tar.gz, .tar.bz2, .tar.xz, .tgz)
    - Audio Transcoding: Converts FLAC, DTS, TrueHD, MLP, ALAC, AAC, Opus, Vorbis, and PCM
    - Language Filtering: Retains specified audio streams (English and Japanese by default)
    - Packaging: Repackages batch media into TAR archives
    - Upload: Uploads files or archives to bzzhr.co via curl

Audio Encoding Specifications:
    - Codec: E-AC-3 (Dolby Digital Plus)
    - Bit rates:
        - 1.0 (Mono): 224 kbit/s
        - 2.0 (Stereo): 224 kbit/s
        - 5.1 (Surround sound, 6 channels): 640 kbit/s
    - Dialnorm: -31 dBFS
    - Surround Mode: Off (for 1.0 and 2.0 channels)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Final, NoReturn, TypedDict

# --------------------------------------------------
# Global Configuration & Constants
# --------------------------------------------------

VERSION: Final[int] = 1

DOWNLOADS_BASE_DIR: Final[Path] = Path.home() / "Downloads"

# Stable folder for in-progress downloads; unlike the temp dir it survives a
# failed run, so aria2c can resume an interrupted download on the next run.
PARTIAL_DOWNLOADS_DIR: Final[Path] = DOWNLOADS_BASE_DIR / "remuxddp-partial"

# Languages to retain (empty set retains all audio streams).
# Note: If none of the selected languages are found in the media file,
# all audio streams are retained and copied as a fallback.
KEEP_LANGUAGES: Final[frozenset[str]] = frozenset({"eng", "jpn"})

CONVERT_AUDIO_CODECS: Final[frozenset[str]] = frozenset({
    "flac",
    "dts",
    "truehd",
    "mlp",
    "alac",
    "aac",
    "opus",
    "vorbis",
    "pcm_s16le",
    "pcm_s24le",
    "pcm_s32le",
    "pcm_f32le",
    "pcm_s16be",
    "pcm_s24be",
    "pcm_bluray",
    "pcm_dvd",
})

MEDIA_EXTENSIONS: Final[frozenset[str]] = frozenset({".mkv", ".mp4", ".webm"})

# Audio codec tokens in release filenames to replace with DDP when transcoding:
# - TrueHD, TrueHD Atmos
# - DTS, DTS-HD MA, DTS-HD HRA, DTS-HD, DTS-MA, DTS-ES, DTS:X, DTS-X
# - AAC, AAC-LC, HE-AAC
# - FLAC, ALAC, OPUS, Vorbis, LPCM, PCM, MLP
_CONVERTIBLE_AUDIO_TOKEN_PATTERNS: Final[tuple[str, ...]] = (
    r"TrueHD(?:[._ -]?Atmos)?",
    r"DTS(?:[._ -]?HD(?:[._ -]?(?:MA|HRA|HR))?|[._ -]?(?:MA|ES|X)|:X)?",
    r"(?:HE[._ -]?)?AAC(?:[._ -]?LC)?",
    r"FLAC",
    r"ALAC",
    r"OPUS",
    r"Vorbis",
    r"L?PCM",
    r"MLP",
)
_JOINED_AUDIO_TOKEN_PATTERNS: Final[str] = "|".join(_CONVERTIBLE_AUDIO_TOKEN_PATTERNS)

AUDIO_CODEC_REPLACE_PATTERN: Final[re.Pattern[str]] = re.compile(
    pattern=rf"(?i)(?<![A-Za-z0-9])(?:{_JOINED_AUDIO_TOKEN_PATTERNS})(?=(?:[1257]\.[01]\b|[^A-Za-z0-9]|$))"
)

# Strips trailing "Atmos" when it appears after the channel layout (e.g., TrueHD.5.1.Atmos -> DDP.5.1)
POST_CHANNEL_ATMOS_PATTERN: Final[re.Pattern[str]] = re.compile(
    pattern=r"(?i)(\bDDP(?:[._ -]?[1257]\.[01])?)[._ -]+Atmos(?![A-Za-z0-9])"
)

# Matches existing Dolby Digital Plus tokens (DDP, DDPA, DDP Atmos, EAC3, E-AC-3, DD+)
EXISTING_DDP_PATTERN: Final[re.Pattern[str]] = re.compile(
    pattern=r"(?i)(?<![A-Za-z0-9])(?:DDPA?(?:[._ -]?Atmos)?|E-?AC-?3(?:[._ -]?Atmos)?|DD\+)(?=(?:[1257]\.[01]\b|[^A-Za-z0-9]|$))"
)

# Marks the end of the show/movie title via Season/Episode (S01E01, S01, E01) or release year
EPISODE_OR_YEAR_PATTERN: Final[re.Pattern[str]] = re.compile(
    pattern=(
        r"(?<![A-Za-z0-9])(?:"
        r"(?i:S\d{1,4}(?:E\d{1,4})?(?:[._ -]?v\d+)?|E\d{1,4}(?:[._ -]?v\d+)?)|"
        r"(?:19|20)\d{2}"
        r")(?![A-Za-z0-9])"
    )
)

# Pre-audio release metadata tokens (Common, Resolution/Remaster, Source, Streaming Service, MULTi)
PRE_AUDIO_TECH_PATTERN: Final[re.Pattern[str]] = re.compile(
    pattern=(
        r"(?<![A-Za-z0-9])(?:"
        r"(?i:PROPER|REPACK|RERIP)|"
        r"(?i:2160p|1080p|720p|576p|480p|4K|UHD|DS4K|RM4K)|"
        r"(?i:Blu-?Ray|BDRemux|REMUX|BDRip|BRRip|WEB-?DL|WEBDL|WEBRip|WEB)|"
        r"(?i:AMZN|APTV|DNSP|DSNP|HULU|PMTP)|"
        r"(?:HBO|MAX|HMAX|HS|IT|JC|MA|NF)|"
        r"(?i:MULTi|Dual[._ -]?Audio)"
        r")(?![A-Za-z0-9])"
    )
)

# Post-audio video codec, HDR/Dolby Vision, and bitrate mode tokens
VIDEO_AND_HDR_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(
    pattern=(
        r"(?<![A-Za-z0-9])(?:"
        r"(?i:x26[45]|h[._-]?26[45]|hevc|avc|av1)|"
        r"(?i:HDR10(?:P|PLUS|\+)|HDRP10\+|HDR10|HDR|HLG|DoVi|DV[._ -]?HDR)|"
        r"(?:DV|CBR)|"
        r"(?i:10[._ -]?bit|Hi10p?)"
        r")(?![A-Za-z0-9])"
    )
)

TEMP_DIR_PREFIX: Final[str] = "remuxddp-"
FFPROBE_TIMEOUT_SECONDS: Final[int] = 60
EXTRACTED_FOLDER_SUFFIX: Final[str] = ".EXTRACTED"
OUTPUT_FOLDER_SUFFIX: Final[str] = ".FFMPEG"

_TAR_EXTENSIONS: Final[tuple[str, ...]] = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tgz",
    ".tbz2",
    ".txz",
    ".tar",
)
_ARCHIVE_EXTENSIONS: Final[tuple[str, ...]] = (".zip", *_TAR_EXTENSIONS)


# --------------------------------------------------
# Stream Metadata & API Response Types
# --------------------------------------------------


class StreamTags(TypedDict, total=False):
    """Metadata tags associated with an ffprobe media stream."""

    language: str
    mimetype: str


class Stream(TypedDict, total=False):
    """Media stream description from ffprobe."""

    index: int
    codec_type: str
    codec_name: str
    channels: int
    tags: StreamTags


class FFProbeResult(TypedDict, total=False):
    """ffprobe root JSON schema."""

    streams: list[Stream]


class BzzhrData(TypedDict, total=False):
    """Nested payload within bzzhr.co upload response."""

    id: str


class BzzhrResponse(TypedDict, total=False):
    """bzzhr.co API JSON response schema."""

    code: int
    data: BzzhrData


# --------------------------------------------------
# Terminal ANSI Formatting & Presentation Helpers
# --------------------------------------------------

ANSI_BOLD: Final[str] = "\033[1m"
ANSI_DIM: Final[str] = "\033[2m"
ANSI_GREEN: Final[str] = "\033[32m"
ANSI_CYAN_BOLD: Final[str] = "\033[1;36m"
ANSI_YELLOW: Final[str] = "\033[33m"
ANSI_RED: Final[str] = "\033[31m"
ANSI_RESET: Final[str] = "\033[0m"


def print_heading(title: str) -> None:
    """Print step heading."""
    print(f"{ANSI_BOLD}{title}{ANSI_RESET}")


def print_operation(description: str) -> None:
    """Print operation details in dim style."""
    print(f"{ANSI_DIM}{description}{ANSI_RESET}")


def print_found_item(path: Path, blank_after: bool = True) -> None:
    """Print detected single media file."""
    ending = "\n" if blank_after else ""
    print(f"\nFound [{ANSI_CYAN_BOLD}{path.name}{ANSI_RESET}]{ending}")


def print_batch_item(position: int, total: int, path: Path) -> None:
    """Print numbered item header for batch processing."""
    print(
        f"{ANSI_BOLD}({position}/{total}){ANSI_RESET} Found [{ANSI_CYAN_BOLD}{path.name}{ANSI_RESET}]"
    )


def print_saved(path: Path) -> None:
    """Print the path of the saved output file."""
    print(f"{ANSI_GREEN}Saved {path}{ANSI_RESET}")


def print_elapsed(seconds: float) -> None:
    """Print the elapsed duration in a human-readable format."""
    rounded = max(0, round(seconds))
    hours, remainder = divmod(rounded, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    parts.append(f"{remaining_seconds}s")
    print(f"{ANSI_DIM}Completed in {' '.join(parts)}{ANSI_RESET}")


def exit_with_error(message: str, code: int = 1) -> NoReturn:
    """Print an error message and exit."""
    sys.stderr.write(f"{ANSI_RED}Error:{ANSI_RESET} {message}\n")
    sys.exit(code)


# --------------------------------------------------
# Dependency & Utility Helpers
# --------------------------------------------------


def validate_required_binary_in_path(binary_name: str) -> str:
    """Ensure an external binary exists on PATH."""
    path: str | None = shutil.which(binary_name)
    if not path:
        exit_with_error(f"The required tool '{binary_name}' wasn't found in PATH.")
    return path


def is_safe_archive_member_path(member_name: str) -> bool:
    """Check for archive folder traversal attempts."""
    path: PurePosixPath = PurePosixPath(member_name)
    return not path.is_absolute() and ".." not in path.parts


def _archive_stem(path: Path) -> str:
    """Extract the base stem of a TAR or ZIP file."""
    lower: str = path.name.lower()
    for suffix in _TAR_EXTENSIONS:
        if lower.endswith(suffix):
            return path.name[: -len(suffix)]
    if lower.endswith(".zip"):
        return path.name[:-4]
    return path.stem


# --------------------------------------------------
# FFprobe Stream Inspection
# --------------------------------------------------


def probe_media_streams_with_ffprobe(media_path: Path) -> list[Stream]:
    """Inspect stream metadata with ffprobe."""
    ffprobe: str = validate_required_binary_in_path("ffprobe")
    command: list[str] = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        ("stream=index,codec_type,codec_name,channels:stream_tags=language,mimetype"),
        "-of",
        "json",
        str(media_path),
    ]

    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            args=command,
            capture_output=True,
            text=True,
            check=False,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        exit_with_error(
            f"ffprobe didn't respond within {FFPROBE_TIMEOUT_SECONDS} seconds "
            f"for '{media_path.name}'."
        )
    if result.returncode != 0:
        error_message: str = result.stderr.strip()
        exit_with_error(
            f"Unable to inspect '{media_path.name}' with ffprobe:\n{error_message}"
        )

    try:
        data: FFProbeResult = json.loads(result.stdout)
        return data.get("streams", [])
    except json.JSONDecodeError:
        exit_with_error(f"Unable to parse ffprobe JSON output for '{media_path.name}'.")


# --------------------------------------------------
# Audio Bit Rate & Filename Formatting
# --------------------------------------------------


def select_audio_streams(streams: Sequence[Stream]) -> list[Stream]:
    """Select audio streams matching KEEP_LANGUAGES, falling back to all audio streams."""
    all_audio_streams: list[Stream] = [
        stream for stream in streams if stream.get("codec_type") == "audio"
    ]
    selected_audio_streams: list[Stream] = [
        stream
        for stream in all_audio_streams
        if not KEEP_LANGUAGES
        or stream.get("tags", {}).get("language", "").lower() in KEEP_LANGUAGES
    ]
    return selected_audio_streams or all_audio_streams


def probe_and_select_audio_streams(
    media_path: Path,
) -> tuple[list[Stream], list[Stream]]:
    """Probe a media file and select its audio streams, exiting when none exist."""
    streams: list[Stream] = probe_media_streams_with_ffprobe(media_path)
    audio_streams: list[Stream] = select_audio_streams(streams)
    if not audio_streams:
        exit_with_error(f"No audio streams were found in '{media_path.name}'.")
    return streams, audio_streams


# E-AC-3 encoding profile (channel layout tag, bit rate) per channel count.
# Unlisted counts (3, 4, 5 channels) fall back to the stereo rate with no layout tag.
EAC3_CHANNEL_PROFILES: Final[dict[int, tuple[str | None, str]]] = {
    1: ("1.0", "224k"),
    2: ("2.0", "224k"),
    6: ("5.1", "640k"),
}
DEFAULT_EAC3_PROFILE: Final[tuple[str | None, str]] = (None, "224k")


def determine_eac3_bit_rate_by_channel_count(channels: int) -> str:
    """Select bit rate for E-AC-3 based on audio channels."""
    if channels > 6:
        exit_with_error(
            f"The FFmpeg E-AC-3 encoder supports up to 6 channels ({channels} requested)."
        )
    return EAC3_CHANNEL_PROFILES.get(channels, DEFAULT_EAC3_PROFILE)[1]


def build_ddp_tag_string(audio_streams: Sequence[Stream], converted: set[str]) -> str:
    """Build the DDP<layout> tags for the converted audio streams."""
    layouts: list[str] = []
    for stream in audio_streams:
        codec_name: str = stream.get("codec_name", "").lower()
        if codec_name in converted:
            channels_count: int = stream.get("channels", 0)
            layout: str | None = EAC3_CHANNEL_PROFILES.get(
                channels_count, DEFAULT_EAC3_PROFILE
            )[0]
            tag: str = f"DDP{layout}" if layout else "DDP"
            if tag not in layouts:
                layouts.append(tag)
    return ".".join(layouts or ["DDP"])


def determine_title_end(stem: str) -> int:
    """Find where the title ends: after the first episode marker or release year."""
    episode_or_year_matches: list[re.Match[str]] = [
        match
        for match in EPISODE_OR_YEAR_PATTERN.finditer(stem)
        if match.start() > 0
    ]
    return episode_or_year_matches[0].end() if episode_or_year_matches else 1


def insert_ddp_tag(stem: str, tag_str: str, title_end: int) -> str:
    """Insert a DDP tag at the appropriate release-metadata position in a stem."""
    pre_matches: list[re.Match[str]] = list(
        PRE_AUDIO_TECH_PATTERN.finditer(stem, pos=title_end)
    )
    search_pos: int = pre_matches[-1].end() if pre_matches else title_end
    video_match: re.Match[str] | None = VIDEO_AND_HDR_TOKEN_PATTERN.search(
        stem, pos=search_pos
    )

    if video_match:
        insert_pos: int = video_match.start()
        if insert_pos > 0 and stem[insert_pos - 1] in "[(":
            insert_pos -= 1
        sep: str = " " if insert_pos > 0 and stem[insert_pos - 1] == " " else "."
        prefix: str = stem[:insert_pos].rstrip(" ._-")
        suffix: str = stem[insert_pos:]
        if prefix:
            return f"{prefix}{sep}{tag_str}{sep}{suffix}"
        return f"{tag_str}{sep}{suffix}"

    if pre_matches:
        insert_pos = pre_matches[-1].end()
        while insert_pos < len(stem) and stem[insert_pos] in "])":
            insert_pos += 1
        sep = " " if (" " in stem and "." not in stem) else "."
        return f"{stem[:insert_pos]}{sep}{tag_str}{stem[insert_pos:]}"

    group_match: re.Match[str] | None = re.search(r"(-[A-Za-z0-9]+)$", stem)
    if group_match and group_match.start() >= title_end:
        return f"{stem[:group_match.start()]}.{tag_str}{group_match.group(1)}"
    sep = " " if (" " in stem and "." not in stem) else "."
    return f"{stem}{sep}{tag_str}"


def generate_ddp_remuxed_filename(
    source_path: Path, audio_streams: Sequence[Stream]
) -> str:
    """Generate output filename replacing or inserting DDP audio tags."""
    converted: set[str] = {
        stream.get("codec_name", "").lower()
        for stream in audio_streams
        if stream.get("codec_name", "").lower() in CONVERT_AUDIO_CODECS
    }

    stem: str = source_path.stem
    if not converted:
        return f"{stem}.mkv"

    original_stem: str = stem

    # 1. Replace existing convertible audio codec tokens in-place with DDP
    stem = AUDIO_CODEC_REPLACE_PATTERN.sub("DDP", stem)
    stem = POST_CHANNEL_ATMOS_PATTERN.sub(r"\1", stem)
    stem = re.sub(r"(?i)\bDDP(?:[._ -]+DDP)+\b", "DDP", stem)

    # 2. If no audio codec token was present in the filename, insert DDP<layout>
    #    at the appropriate release-metadata position.
    has_ddp: bool = bool(EXISTING_DDP_PATTERN.search(stem))
    if stem == original_stem and not has_ddp:
        tag_str: str = build_ddp_tag_string(audio_streams, converted)
        stem = insert_ddp_tag(stem, tag_str, determine_title_end(stem))

    return f"{stem}.mkv"


# --------------------------------------------------
# Build FFmpeg Command & Remux
# --------------------------------------------------


def construct_ffmpeg_ddp_remux_command(
    source_path: Path,
    output_path: Path,
    streams: Sequence[Stream],
) -> list[str]:
    """Build the FFmpeg command to copy video and transcode audio to E-AC-3."""
    ffmpeg: str = validate_required_binary_in_path("ffmpeg")
    command: list[str] = [
        ffmpeg,
        "-hide_banner",
        "-n",
        "-i",
        str(source_path),
        "-map",
        "0:v?",
    ]

    selected_audio_streams: list[Stream] = select_audio_streams(streams)

    for stream in selected_audio_streams:
        stream_index: int = stream.get("index", 0)
        command.extend(["-map", f"0:{stream_index}"])

    command.extend([
        "-map",
        "0:s?",
        "-map",
        "0:d?",
        "-map",
        "0:t?",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-c:v",
        "copy",
        "-c:s",
        "copy",
        "-c:d",
        "copy",
        "-c:t",
        "copy",
    ])

    for output_index, stream in enumerate(selected_audio_streams):
        codec: str = stream.get("codec_name", "").lower()
        if codec in CONVERT_AUDIO_CODECS:
            channels: int = stream.get("channels", 2)
            bit_rate: str = determine_eac3_bit_rate_by_channel_count(channels)
            language: str = stream.get("tags", {}).get("language", "")
            command.extend([
                f"-c:a:{output_index}",
                "eac3",
                f"-b:a:{output_index}",
                bit_rate,
                f"-dialnorm:a:{output_index}",
                "-31",
            ])
            if channels <= 2:
                command.extend([
                    f"-dsur_mode:a:{output_index}",
                    "off",
                ])
            if language:
                command.extend([
                    f"-metadata:s:a:{output_index}",
                    f"language={language}",
                ])
        else:
            command.extend([
                f"-c:a:{output_index}",
                "copy",
            ])

    command.append(str(output_path))
    return command


def remux_single_media_file(
    source_path: Path,
    output_path: Path,
    streams: Sequence[Stream] | None = None,
) -> None:
    """Remux a single media file to MKV with E-AC-3 audio."""
    resolved_streams: Sequence[Stream] = (
        streams
        if streams is not None
        else probe_media_streams_with_ffprobe(source_path)
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        exit_with_error(
            f"The output file already exists: {output_path}. "
            "Two inputs may map to the same output name."
        )
    command: list[str] = construct_ffmpeg_ddp_remux_command(
        source_path=source_path,
        output_path=output_path,
        streams=resolved_streams,
    )
    print(shlex.join(command))

    result: subprocess.CompletedProcess[bytes] = subprocess.run(
        args=command, check=False
    )
    if (
        result.returncode != 0
        or not output_path.is_file()
        or output_path.stat().st_size == 0
    ):
        exit_with_error(
            f"Unable to remux '{source_path.name}' with FFmpeg (exit code {result.returncode})."
        )

    print()


# --------------------------------------------------
# Archive Extraction & Packaging
# --------------------------------------------------


def safely_extract_media_archive(archive_path: Path, working_folder: Path) -> Path:
    """Extract a ZIP or TAR archive safely."""
    destination_path: Path = (
        working_folder / f"{_archive_stem(archive_path)}{EXTRACTED_FOLDER_SUFFIX}"
    )
    if destination_path.exists():
        exit_with_error(f"Extraction destination already exists: {destination_path}.")
    destination_path.mkdir(parents=True, exist_ok=True)
    lower: str = archive_path.name.lower()
    backend: str = "zipfile" if lower.endswith(".zip") else "tarfile"

    print_heading("Extracting")
    print_operation(
        f"Python {backend}: extract {archive_path.name} -> {destination_path.name}"
    )

    if lower.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zip_file:
            for item in zip_file.infolist():
                if not is_safe_archive_member_path(item.filename):
                    exit_with_error(
                        f"The archive contains an unsafe path: '{item.filename}'."
                    )
                zip_file.extract(member=item, path=destination_path)
    elif lower.endswith(_TAR_EXTENSIONS):
        with tarfile.open(archive_path) as tar_file:
            for member in tar_file.getmembers():
                if not is_safe_archive_member_path(member.name):
                    exit_with_error(
                        f"The archive contains an unsafe path: '{member.name}'."
                    )
            try:
                tar_file.extractall(path=destination_path, filter="data")
            except TypeError:
                tar_file.extractall(path=destination_path)
    else:
        exit_with_error(
            "Unsupported archive format. Use a ZIP (.zip) or TAR (.tar.gz, .tar.bz2, .tar.xz) file."
        )

    visible: list[Path] = [
        path for path in destination_path.iterdir() if not path.name.startswith(".")
    ]
    return visible[0] if len(visible) == 1 and visible[0].is_dir() else destination_path


def create_tar_archive_from_folder(folder_path: Path) -> Path:
    """Package folder into a .tar archive for uploading."""
    archive: Path = folder_path.with_name(f"{folder_path.name}.tar")
    if archive.exists():
        exit_with_error(f"Archive destination already exists: {archive}.")

    print_heading("Archiving")
    print_operation(f"Python tarfile: archive {folder_path.name} -> {archive.name}")
    with tarfile.open(archive, mode="w") as tar_file:
        for file_path in sorted(folder_path.rglob("*")):
            if file_path.is_file():
                tar_file.add(file_path, arcname=file_path.relative_to(folder_path))
    print_saved(archive)
    print()
    return archive


# --------------------------------------------------
# Download & Remote Upload
# --------------------------------------------------


def download_remote_file_with_fallback(url: str, destination_folder: Path) -> Path:
    """Download a file using aria2c or curl."""
    aria2c: str | None = shutil.which("aria2c")
    curl: str | None = shutil.which("curl")

    print_heading("Downloading")
    command: list[str]
    if aria2c:
        command = [
            aria2c,
            "--continue=true",
            # Restart from scratch when the target exists without its .aria2
            # control file (a finished download from an earlier failed run).
            "--allow-overwrite=true",
            "--content-disposition=true",
            "--max-connection-per-server=16",
            "--split=16",
            f"--dir={destination_folder}",
            url,
        ]
    elif curl:
        command = [curl, "-L", "-O", "--output-dir", str(destination_folder), url]
    else:
        exit_with_error("Install aria2 or curl to download files from URLs.")

    print(shlex.join(command))
    result: subprocess.CompletedProcess[bytes] = subprocess.run(
        args=command, check=False
    )
    if result.returncode != 0:
        exit_with_error(f"Download failed with exit code {result.returncode}.")

    print()
    downloaded_files: list[Path] = [
        path
        for path in destination_folder.iterdir()
        if not path.name.endswith(".aria2")
    ]
    if not downloaded_files:
        exit_with_error("No file was downloaded.")
    if len(downloaded_files) > 1:
        file_names: str = ", ".join(path.name for path in sorted(downloaded_files))
        exit_with_error(
            "The download produced more than one file "
            f"({file_names}); can't tell which one to process."
        )
    return downloaded_files[0]


def upload_media_file_to_bzzhr(file_to_upload: Path) -> str:
    """Upload file to bzzhr.co and return download link."""
    curl: str = validate_required_binary_in_path("curl")
    encoded_name: str = urllib.parse.quote(file_to_upload.name)
    upload_url: str = f"https://w.bzzhr.co/{encoded_name}"

    print_heading("Uploading")
    command: list[str] = [
        curl,
        "-o",
        "-",
        "-T",
        str(file_to_upload),
        upload_url,
    ]

    print(shlex.join(command))
    # Capture stdout for the JSON response while leaving stderr attached to the
    # terminal so curl renders its live transfer stats (speed, ETA, bytes).
    result: subprocess.CompletedProcess[str] = subprocess.run(
        args=command,
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        exit_with_error(f"bzzhr.co upload failed (exit code {result.returncode}).")

    try:
        response: BzzhrResponse = json.loads(result.stdout)
        response_data: BzzhrData | None = response.get("data")
        file_id: str | None = (
            response_data.get("id")
            if isinstance(response_data, dict)
            else None
        )
        if not file_id:
            exit_with_error(
                "The bzzhr.co upload response didn't include a file ID."
            )
        return f"https://bzzhr.co/{file_id}"
    except json.JSONDecodeError:
        exit_with_error("Unable to parse bzzhr.co upload response.")


# --------------------------------------------------
# Main Workflow Execution
# --------------------------------------------------


def create_output_folder(working_folder: Path, base_name: str) -> Path:
    """Create the .FFMPEG output folder inside the working folder."""
    output_folder: Path = working_folder / f"{base_name}{OUTPUT_FOLDER_SUFFIX}"
    output_folder.mkdir(parents=True, exist_ok=True)
    return output_folder


def partial_download_folder_for_url(url: str) -> Path:
    """Return the stable per-URL folder used for resumable downloads."""
    url_key: str = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return PARTIAL_DOWNLOADS_DIR / url_key


def remove_partial_download(downloaded_path: Path) -> None:
    """Delete an uploaded download, keeping the folder only if partials remain."""
    downloaded_path.unlink(missing_ok=True)
    downloaded_path.with_name(f"{downloaded_path.name}.aria2").unlink(missing_ok=True)
    try:
        downloaded_path.parent.rmdir()
    except OSError:
        pass


def acquire_source_path(input_arg: str) -> tuple[Path, bool]:
    """Resolve the input to a local path and whether it lives in the partial folder."""
    parsed: urllib.parse.ParseResult = urllib.parse.urlparse(input_arg)

    if parsed.scheme in {"http", "https"}:
        # Downloads live in a stable per-URL folder (never the temp dir) so
        # aria2c's --continue can resume an interrupted download when the same
        # URL is re-run; the file is deleted only after a successful upload.
        download_folder: Path = partial_download_folder_for_url(input_arg)
        download_folder.mkdir(parents=True, exist_ok=True)

        completed: list[Path] = [
            path
            for path in download_folder.iterdir()
            if not path.name.endswith(".aria2")
        ]
        if len(completed) == 1 and not completed[0].with_name(
            f"{completed[0].name}.aria2"
        ).exists():
            # Finished download from an earlier run that failed later; reuse it.
            print_found_item(completed[0])
            print_heading("Reusing previous download")
            print()
            return completed[0], True

        downloaded_path: Path = download_remote_file_with_fallback(
            input_arg, download_folder
        )
        print_saved(downloaded_path)
        print()
        return downloaded_path, True

    local_path: Path = Path(input_arg).expanduser()
    if not local_path.exists():
        if parsed.scheme:
            exit_with_error(
                f"Unsupported URL scheme '{parsed.scheme}:'. "
                "Use http://, https://, or a local file path."
            )
        exit_with_error(f"The file wasn't found: {local_path}.")

    # Local inputs are read in place; only outputs are written to the temp folder.
    print_found_item(local_path)
    print_heading("Using local input")
    print()
    return local_path, False


def process_single_media_file(source_path: Path, working_folder: Path) -> Path:
    """Remux one media file and return the output file to upload."""
    streams, audio_streams = probe_and_select_audio_streams(source_path)
    output_folder: Path = create_output_folder(working_folder, source_path.stem)
    output_file: Path = output_folder / generate_ddp_remuxed_filename(
        source_path, audio_streams
    )
    print_heading("Remuxing")
    print_found_item(source_path, blank_after=False)
    remux_single_media_file(source_path, output_file, streams=streams)
    print_saved(output_file)
    print()
    return output_file


def process_media_archive(archive_path: Path, working_folder: Path) -> Path:
    """Extract an archive, remux each media file, and return the repacked TAR."""
    extracted_folder: Path = safely_extract_media_archive(archive_path, working_folder)
    output_folder: Path = create_output_folder(working_folder, extracted_folder.name)

    media_files_list: list[Path] = sorted(
        path
        for path in extracted_folder.rglob("*")
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
    )
    if not media_files_list:
        exit_with_error("No media files were found in the archive.")

    print()
    print_heading("Remuxing")
    total_media_files: int = len(media_files_list)
    # One unreadable file aborts the whole run on purpose: a partial archive
    # must never be uploaded.
    for position, file_path in enumerate(media_files_list, 1):
        file_streams, file_audio = probe_and_select_audio_streams(file_path)
        relative_path: Path = file_path.relative_to(extracted_folder).with_name(
            generate_ddp_remuxed_filename(file_path, file_audio)
        )
        output_path: Path = output_folder / relative_path
        print_batch_item(position, total_media_files, file_path)
        remux_single_media_file(file_path, output_path, streams=file_streams)
        print_saved(output_path)
        print()

    # The extracted originals are dead weight once every file is remuxed, and
    # the loose outputs are dead weight once the TAR exists; removing each
    # keeps peak disk usage at roughly two copies of the media.
    shutil.rmtree(extracted_folder)
    archive: Path = create_tar_archive_from_folder(output_folder)
    shutil.rmtree(output_folder)
    return archive


def main() -> None:
    """Execute the primary remuxddp command-line workflow."""
    parser = argparse.ArgumentParser(
        description="Acquire media, remux audio to Dolby Digital Plus (E-AC-3), and upload"
    )
    parser.add_argument(
        "source",
        help="URL, archive, or media file to process.",
    )
    args: argparse.Namespace = parser.parse_args()

    started: float = time.monotonic()
    print(
        f"\n{ANSI_BOLD}remuxddp v{VERSION}{ANSI_RESET}: "
        "Remux and upload a media file or collection"
    )
    print(
        "Converts FLAC, DTS, TrueHD, MLP, ALAC, AAC, Opus, Vorbis, and PCM audio to E-AC-3."
    )

    input_arg: str = args.source
    DOWNLOADS_BASE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(
            prefix=TEMP_DIR_PREFIX, dir=DOWNLOADS_BASE_DIR
        ) as temporary_folder:
            working_folder: Path = Path(temporary_folder)
            print(f"\nWorking folder: {working_folder}")

            source_path, was_downloaded = acquire_source_path(input_arg)
            is_archive: bool = source_path.name.lower().endswith(_ARCHIVE_EXTENSIONS)

            if is_archive:
                upload_target: Path = process_media_archive(source_path, working_folder)
            else:
                upload_target = process_single_media_file(
                    source_path, working_folder
                )

            download_page: str = upload_media_file_to_bzzhr(upload_target)
            print(f"{ANSI_GREEN}Download page:{ANSI_RESET} {download_page}")
            print_elapsed(time.monotonic() - started)

            if was_downloaded:
                remove_partial_download(source_path)
    except KeyboardInterrupt:
        sys.stderr.write(
            f"{ANSI_YELLOW}Canceled. Incomplete files were removed.{ANSI_RESET}\n"
        )
        sys.exit(130)
    except OSError as error:
        exit_with_error(f"Filesystem error: {error}")


if __name__ == "__main__":
    main()
