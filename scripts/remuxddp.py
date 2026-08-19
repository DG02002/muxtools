"""Acquire media, remux audio to Dolby Digital Plus (E-AC-3), and upload.

Downloads remote files or archives using aria2c (with curl fallback), extracts
ZIP and TAR archives, remuxes audio streams to Dolby Digital Plus, repackages
archives, and uploads the output to Gofile using curl.

Pipeline Workflow:
    - Download: Accelerated transfer via aria2c (fallback to curl)
    - Archive Extraction: Unpacks ZIP (.zip) and TAR (.tar, .tar.gz, .tar.bz2, .tar.xz, .tgz)
    - Audio Transcoding: Converts lossless/surround codecs (FLAC, DTS, TrueHD, ALAC, AAC)
    - Language Filtering: Retains specified audio streams (English and Japanese by default)
    - Packaging: Repackages batch media into TAR archives
    - Upload: Posts files or archives to Gofile via curl

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
import json
import os
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

# Languages to retain (empty set retains all audio streams).
# Note: If none of the selected languages are found in the media file,
# all audio streams are retained and copied as a fallback.
KEEP_LANGUAGES: Final[frozenset[str]] = frozenset({"eng", "jpn"})

CONVERT_AUDIO_CODECS: Final[frozenset[str]] = frozenset({
    "flac",
    "dts",
    "truehd",
    "alac",
    "aac",
})

MEDIA_EXTENSIONS: Final[frozenset[str]] = frozenset({".mkv", ".mp4", ".webm"})

CODEC_TOKENS: Final[dict[str, str]] = {
    "flac": "FLAC",
    "dts": "DTS",
    "truehd": "TrueHD",
    "alac": "ALAC",
    "aac": "AAC",
}

VIDEO_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(
    pattern=(
        r"(?i)(?<![A-Za-z0-9])"
        r"(?:x26[45]|h[._-]?26[45]|hevc|av1)"
        r"(?![A-Za-z0-9])"
    )
)

TEMP_DIR_PREFIX: Final[str] = "remuxddp-"
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


class GoFileData(TypedDict, total=False):
    """Nested payload within Gofile upload response."""

    downloadPage: str


class GoFileResponse(TypedDict, total=False):
    """Gofile API JSON response schema."""

    status: str
    data: GoFileData


# --------------------------------------------------
# Terminal ANSI Formatting & Presentation Helpers
# --------------------------------------------------

ANSI_BOLD: Final[str] = "\033[1m"
ANSI_DIM: Final[str] = "\033[2m"
ANSI_GREEN: Final[str] = "\033[32m"
ANSI_GREEN_BOLD: Final[str] = "\033[1;32m"
ANSI_CYAN_BOLD: Final[str] = "\033[1;36m"
ANSI_YELLOW: Final[str] = "\033[33m"
ANSI_RED: Final[str] = "\033[31m"
ANSI_RESET: Final[str] = "\033[0m"


def print_script_intro(name: str, description: str, detail: str | None = None) -> None:
    """Print the standard script header."""
    print(f"\n{ANSI_BOLD}{name} v{VERSION}{ANSI_RESET}: {description}")
    if detail:
        print(detail)


def print_working_folder(path: Path) -> None:
    """Print active working temporary folder."""
    print(f"\nWorking folder: {path}")


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


def print_uploaded(link: str) -> None:
    """Print uploaded download link."""
    print(f"{ANSI_GREEN}Download page:{ANSI_RESET} {link}")


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


def print_error(message: str) -> None:
    """Print formatted error to stderr."""
    sys.stderr.write(f"{ANSI_RED}Error:{ANSI_RESET} {message}\n")


def print_canceled() -> None:
    """Print a cancellation notice."""
    sys.stderr.write(
        f"{ANSI_YELLOW}Canceled. Incomplete files were removed.{ANSI_RESET}\n"
    )


def exit_with_error(message: str, code: int = 1) -> NoReturn:
    """Print an error message and exit."""
    print_error(message)
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

    result: subprocess.CompletedProcess[str] = subprocess.run(
        args=command,
        capture_output=True,
        text=True,
        check=False,
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
        return []


# --------------------------------------------------
# Audio Bit Rate & Filename Formatting
# --------------------------------------------------


def determine_eac3_bit_rate_by_channel_count(channels: int) -> str:
    """Select bit rate for E-AC-3 based on audio channels."""
    if channels > 6:
        exit_with_error(
            f"The FFmpeg E-AC-3 encoder supports up to 6 channels ({channels} requested)."
        )
    if channels == 6:
        return "640k"
    if channels == 1:
        return "224k"
    return "224k"


def generate_ddp_remuxed_filename(
    source_path: Path, audio_streams: Sequence[Stream]
) -> str:
    """Generate output filename replacing audio tags (such as FLAC to DDP5.1)."""
    converted: set[str] = {
        stream.get("codec_name", "").lower()
        for stream in audio_streams
        if stream.get("codec_name", "").lower() in CONVERT_AUDIO_CODECS
    }

    stem: str = source_path.stem
    original_stem: str = stem

    for codec in sorted(converted, key=len, reverse=True):
        token: str = CODEC_TOKENS[codec]
        stem = re.sub(
            pattern=rf"(?i)(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])",
            repl="DDP",
            string=stem,
        )

    has_ddp: bool = bool(re.search(r"(?i)(?<![A-Za-z])DDP(?![A-Za-z])", stem))
    if converted and stem == original_stem and not has_ddp:
        layouts: list[str] = []
        for stream in audio_streams:
            codec_name: str = stream.get("codec_name", "").lower()
            if codec_name in converted:
                channels_count: int = stream.get("channels", 0)
                layout: str | None = {
                    1: "1.0",
                    2: "2.0",
                    6: "5.1",
                }.get(channels_count)
                tag: str = f"DDP{layout}" if layout else "DDP"
                if tag not in layouts:
                    layouts.append(tag)

        tag_str: str = ".".join(layouts or ["DDP"])
        match: re.Match[str] | None = VIDEO_TOKEN_PATTERN.search(stem)
        if match:
            sep: str = "" if match.start() == 0 else "."
            prefix: str = stem[: match.start()].rstrip(" ._-")
            suffix: str = stem[match.start() :]
            stem = f"{prefix}{sep}{tag_str}.{suffix}"
        else:
            stem = f"{stem}.{tag_str}"

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

    all_audio_streams: list[Stream] = [
        stream for stream in streams if stream.get("codec_type") == "audio"
    ]
    selected_audio_streams: list[Stream] = [
        stream
        for stream in all_audio_streams
        if not KEEP_LANGUAGES
        or stream.get("tags", {}).get("language", "").lower() in KEEP_LANGUAGES
    ]
    if not selected_audio_streams:
        selected_audio_streams = all_audio_streams

    for stream in selected_audio_streams:
        stream_index: int = stream.get("index", 0)
        command.extend(["-map", f"0:{stream_index}"])

    command.extend([
        "-map",
        "0:s?",
        "-map",
        "0:d?",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-c",
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

    command.append(str(output_path))
    return command


def remux_single_media_file(source_path: Path, output_path: Path) -> None:
    """Remux a single media file to MKV with E-AC-3 audio."""
    streams: list[Stream] = probe_media_streams_with_ffprobe(source_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command: list[str] = construct_ffmpeg_ddp_remux_command(
        source_path=source_path,
        output_path=output_path,
        streams=streams,
    )
    print(shlex.join(command))

    result: subprocess.CompletedProcess[bytes] = subprocess.run(
        args=command, check=False
    )
    if result.returncode != 0:
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
    before: set[Path] = (
        set(destination_folder.iterdir()) if destination_folder.exists() else set()
    )
    aria2c: str | None = shutil.which("aria2c")
    curl: str | None = shutil.which("curl")

    print_heading("Downloading")
    command: list[str]
    if aria2c:
        command = [
            aria2c,
            "--continue=true",
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
    after: set[Path] = set(destination_folder.iterdir())
    new_files: list[Path] = [
        path for path in after - before if not path.name.endswith(".aria2")
    ]
    if not new_files:
        exit_with_error("No file was downloaded.")
    return min(new_files)


def upload_media_file_to_gofile(file_to_upload: Path) -> str:
    """Upload file to Gofile and return download link."""
    curl: str = validate_required_binary_in_path("curl")
    token: str | None = os.environ.get("GOFILE_TOKEN")

    print_heading("Uploading")
    command: list[str] = [
        curl,
        "--fail-with-body",
        "--location",
        "--request",
        "POST",
        "--form",
        f"file=@{file_to_upload}",
    ]
    if token:
        command.extend(["--header", f"Authorization: Bearer {token}"])
    command.append("https://upload.gofile.io/uploadfile")

    print(shlex.join(command))
    result: subprocess.CompletedProcess[str] = subprocess.run(
        args=command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        error_message: str = result.stderr.strip()
        exit_with_error(f"Gofile upload failed:\n{error_message}")

    try:
        response: GoFileResponse = json.loads(result.stdout)
        response_data: GoFileData | None = response.get("data")
        link: str | None = (
            response_data.get("downloadPage")
            if isinstance(response_data, dict)
            else None
        )
        if not link:
            exit_with_error(
                "The Gofile upload response didn't include a download page link."
            )
        return str(link)
    except json.JSONDecodeError:
        exit_with_error("Unable to parse Gofile upload response.")
        return ""


# --------------------------------------------------
# Main Workflow Execution
# --------------------------------------------------


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
    print_script_intro(
        "remuxddp",
        "Remux and upload a media file or collection",
        detail="Converts FLAC, DTS, TrueHD, ALAC, and AAC audio to E-AC-3.",
    )

    input_arg: str = args.source
    DOWNLOADS_BASE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(
            prefix=TEMP_DIR_PREFIX, dir=DOWNLOADS_BASE_DIR
        ) as temporary_folder:
            working_folder: Path = Path(temporary_folder)
            print_working_folder(working_folder)
            parsed: urllib.parse.ParseResult = urllib.parse.urlparse(input_arg)

            # Acquire input
            source_path: Path
            if parsed.scheme in {"http", "https"}:
                source_path = download_remote_file_with_fallback(
                    input_arg, working_folder
                )
                print_saved(source_path)
                print()
            else:
                local: Path = Path(input_arg).expanduser()
                if not local.exists():
                    exit_with_error(f"The file wasn't found: {local}.")
                source_path = working_folder / local.name
                print_found_item(local)
                print_heading("Using local input")
                shutil.copy2(local, source_path)
                print_saved(source_path)
                print()

            lower_name: str = source_path.name.lower()
            is_archive: bool = lower_name.endswith(_ARCHIVE_EXTENSIONS)

            # Process input
            upload_target: Path
            if not is_archive:
                streams: list[Stream] = probe_media_streams_with_ffprobe(source_path)
                audio_streams: list[Stream] = [
                    stream for stream in streams if stream.get("codec_type") == "audio"
                ]
                if not audio_streams:
                    exit_with_error(
                        f"No audio streams were found in '{source_path.name}'."
                    )
                output_file: Path = working_folder / generate_ddp_remuxed_filename(
                    source_path, audio_streams
                )
                print_heading("Remuxing")
                print_found_item(source_path, blank_after=False)
                remux_single_media_file(source_path, output_file)
                print_saved(output_file)
                print()
                upload_target = output_file
            else:
                extracted_folder: Path = safely_extract_media_archive(
                    source_path, working_folder
                )
                output_folder: Path = (
                    working_folder / f"{extracted_folder.name}{OUTPUT_FOLDER_SUFFIX}"
                )
                output_folder.mkdir(parents=True, exist_ok=True)

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
                for position, file_path in enumerate(media_files_list, 1):
                    file_streams: list[Stream] = probe_media_streams_with_ffprobe(
                        file_path
                    )
                    file_audio: list[Stream] = [
                        stream
                        for stream in file_streams
                        if stream.get("codec_type") == "audio"
                    ]
                    relative_path: Path = file_path.relative_to(
                        extracted_folder
                    ).with_name(generate_ddp_remuxed_filename(file_path, file_audio))
                    output_path: Path = output_folder / relative_path
                    print_batch_item(position, total_media_files, file_path)
                    remux_single_media_file(file_path, output_path)
                    print_saved(output_path)
                    print()

                upload_target = create_tar_archive_from_folder(output_folder)

            # Upload
            download_page: str = upload_media_file_to_gofile(upload_target)
            print_uploaded(download_page)
            print_elapsed(time.monotonic() - started)
    except KeyboardInterrupt:
        print_canceled()
        sys.exit(130)


if __name__ == "__main__":
    main()
