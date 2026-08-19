"""Remux media files into QuickTime-compatible MP4 containers.

Remuxes video and compatible audio into standard MP4 containers with
appropriate FourCC tags and metadata for Apple QuickTime and AVFoundation.

Specifications & Stream Handling:
    - Video:
        - Stream: Passthrough copy (-c:v copy)
        - FourCC tags: avc1 (H.264), hvc1 (HEVC), av01 (AV1), vc-1 (VC-1), dva1/dvh1 (Dolby Vision)
    - Audio:
        - Passthrough codecs: AAC, Apple Lossless (ALAC), AC-3, E-AC-3, MP3 (-c:a copy)
        - Fallback codec: Apple Lossless (lossless transcode for unsupported audio)
    - Subtitles:
        - Text subtitles converted to QuickTime mov_text (such as SRT, ASS/SSA, and WebVTT)
    - Container:
        - Brand: mp42
        - Flags: +faststart
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final, NoReturn, TypedDict

# --------------------------------------------------
# Global Configuration
# --------------------------------------------------

VERSION: Final[int] = 1

TEXT_SUBTITLE_CODECS: Final[frozenset[str]] = frozenset({
    "mov_text",
    "subrip",
    "srt",
    "ass",
    "ssa",
    "webvtt",
    "text",
})
TARGET_SUBTITLE_CODEC: Final[str] = "mov_text"

MEDIA_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".mkv",
    ".mp4",
    ".m4v",
    ".mov",
    ".webm",
    ".avi",
})

# FourCC tags required by QuickTime Player and AVFoundation
VIDEO_TAGS: Final[dict[str, str]] = {
    "h264": "avc1",
    "hevc": "hvc1",
    "av1": "av01",
    "vc1": "vc-1",
}
DOLBY_VISION_TAGS: Final[dict[tuple[str, int], str]] = {
    ("h264", 9): "dva1",
    ("hevc", 5): "dvh1",
}

MP4_MOVFLAGS: Final[str] = "+faststart"
MP4_BRAND: Final[str] = "mp42"
MP4_STRICTNESS: Final[str] = "unofficial"

TEMP_DIR_PREFIX: Final[str] = ".quickmp4-"
FOLDER_OUTPUT_SUFFIX: Final[str] = ".quickmp4"


# --------------------------------------------------
# Stream Metadata Types
# --------------------------------------------------


class SideData(TypedDict, total=False):
    """Side data metadata extracted from ffprobe."""

    side_data_type: str
    dv_profile: int


class Stream(TypedDict, total=False):
    """Individual stream metadata returned by ffprobe."""

    index: int
    codec_type: str
    codec_name: str
    disposition: dict[str, int]
    side_data_list: list[SideData]


class FFProbeResult(TypedDict, total=False):
    """Overall JSON response structure from ffprobe."""

    streams: list[Stream]


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


def print_found_item(path: Path, blank_after: bool = True) -> None:
    """Print a single detected media file."""
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


def print_skipped(path: Path) -> None:
    """Print skipped output path message."""
    print(f"{ANSI_YELLOW}Skipped{ANSI_RESET} [{path}]")


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
# Dependency & Binary Verification
# --------------------------------------------------


def validate_required_binary_in_path(binary_name: str) -> str:
    """Ensure an external binary exists on PATH."""
    path: str | None = shutil.which(cmd=binary_name)
    if not path:
        exit_with_error(f"The required tool '{binary_name}' wasn't found in PATH.")
    return path


# --------------------------------------------------
# FFprobe Stream Inspection
# --------------------------------------------------


def probe_media_streams_with_ffprobe(source_path: Path) -> list[Stream]:
    """Inspect stream information using ffprobe."""
    ffprobe: str = validate_required_binary_in_path(binary_name="ffprobe")
    command: list[str] = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        (
            "stream=index,codec_type,codec_name:"
            "stream_disposition:"
            "stream_side_data=side_data_type,dv_profile"
        ),
        "-of",
        "json",
        str(object=source_path),
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
            f"Unable to inspect '{source_path.name}' with ffprobe:\n{error_message}"
        )

    try:
        data: FFProbeResult = json.loads(s=result.stdout)
        return data.get("streams", [])
    except json.JSONDecodeError:
        exit_with_error(
            f"Unable to parse ffprobe JSON output for '{source_path.name}'."
        )
        return []


# --------------------------------------------------
# FourCC Tag & Path Resolution
# --------------------------------------------------


def determine_quicktime_video_tag(video_stream: Stream) -> str:
    """Determine the QuickTime FourCC video tag (such as hvc1, avc1, or dva1)."""
    codec: str = str(object=video_stream.get("codec_name", ""))
    side_data_list: list[SideData] = video_stream.get("side_data_list", [])

    dv_profile: int | None = next(
        (
            item.get("dv_profile")
            for item in side_data_list
            if item.get("side_data_type") == "DOVI configuration record"
        ),
        None,
    )

    tag: str | None = None
    if dv_profile is not None:
        tag = DOLBY_VISION_TAGS.get((codec, dv_profile))
    if not tag:
        tag = VIDEO_TAGS.get(codec)

    if not tag:
        exit_with_error(
            f"The video codec '{codec}' isn't supported in QuickTime-compatible MP4 files."
        )
    return tag


def generate_default_output_path(source_path: Path) -> Path:
    """Generate default output path with .mp4 extension."""
    re_remux_mp4_suffix: str = ".quicktime.mp4"
    suffix: str = (
        re_remux_mp4_suffix if source_path.suffix.lower() == ".mp4" else ".mp4"
    )
    return source_path.with_suffix(suffix)


def recursively_find_supported_media_files(folder_path: Path) -> list[Path]:
    """Recursively collect supported media files from a folder."""
    return sorted(
        path
        for path in folder_path.rglob(pattern="*")
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
    )


# --------------------------------------------------
# Build FFmpeg Command
# --------------------------------------------------


def construct_ffmpeg_remux_command(
    source_path: Path,
    output_path: Path,
    streams: Sequence[Stream],
) -> list[str]:
    """Build the FFmpeg command-line arguments."""
    ffmpeg: str = validate_required_binary_in_path(binary_name="ffmpeg")

    # Select main video stream, ignoring attached cover art
    video: Stream | None = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "video"
            and not stream.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    if not video:
        exit_with_error(f"No video stream was found in '{source_path.name}'.")

    video_index: int = video.get("index", 0)
    video_tag: str = determine_quicktime_video_tag(video_stream=video)

    command: list[str] = [
        ffmpeg,
        "-hide_banner",
        "-n",
        "-i",
        str(object=source_path),
        "-map",
        f"0:{video_index}",
        "-c:v",
        "copy",
        "-tag:v:0",
        video_tag,
    ]

    audio_index: int = 0
    subtitle_index: int = 0

    copy_audio_codecs: frozenset[str] = frozenset({
        "aac",
        "alac",
        "ac3",
        "eac3",
        "mp3",
    })
    fallback_audio_codec: str = "alac"

    for stream in streams:
        codec_type: str = str(object=stream.get("codec_type", ""))
        codec_name: str = str(object=stream.get("codec_name", ""))
        stream_index: int = stream.get("index", 0)

        if codec_type == "audio":
            audio_codec: str = (
                "copy" if codec_name in copy_audio_codecs else fallback_audio_codec
            )
            command.extend([
                "-map",
                f"0:{stream_index}",
                f"-c:a:{audio_index}",
                audio_codec,
            ])
            audio_index += 1

        elif codec_type == "subtitle" and codec_name in TEXT_SUBTITLE_CODECS:
            subtitle_codec: str = (
                "copy" if codec_name == TARGET_SUBTITLE_CODEC else TARGET_SUBTITLE_CODEC
            )
            command.extend([
                "-map",
                f"0:{stream_index}",
                f"-c:s:{subtitle_index}",
                subtitle_codec,
            ])
            subtitle_index += 1

    command.extend([
        "-map_chapters",
        "0",
        "-map_metadata",
        "0",
        "-dn",
        "-strict",
        MP4_STRICTNESS,
        "-movflags",
        MP4_MOVFLAGS,
        "-brand",
        MP4_BRAND,
        str(object=output_path),
    ])
    return command


# --------------------------------------------------
# Remux Execution & Job Planning
# --------------------------------------------------


def remux_single_media_file(source_path: Path, output_path: Path) -> None:
    """Remux a single media file to MP4 using an atomic temporary write."""
    if not source_path.is_file():
        exit_with_error(f"The input file wasn't found: {source_path}.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    streams: list[Stream] = probe_media_streams_with_ffprobe(source_path)

    with tempfile.TemporaryDirectory(
        prefix=TEMP_DIR_PREFIX, dir=output_path.parent
    ) as temp_dir:
        temp_output: Path = Path(temp_dir) / output_path.name
        command: list[str] = construct_ffmpeg_remux_command(
            source_path=source_path,
            output_path=temp_output,
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
        os.replace(src=temp_output, dst=output_path)


def plan_jobs(sources: Sequence[Path], output: Path | None) -> list[tuple[Path, Path]]:
    """Plan source and target paths for single and batch remuxing."""
    jobs: list[tuple[Path, Path]] = []
    multiple_sources: bool = len(sources) > 1

    for source in sources:
        if source.is_file():
            if output is None:
                destination = generate_default_output_path(source_path=source)
            elif multiple_sources:
                destination = output / generate_default_output_path(
                    source_path=Path(source.name)
                )
            else:
                destination = output
            jobs.append((source, destination))
            continue

        if not source.is_dir():
            exit_with_error(f"The input isn't a valid file or folder: {source}.")

        destination_folder = (
            output / source.name
            if output is not None and multiple_sources
            else output or source.with_name(f"{source.name}{FOLDER_OUTPUT_SUFFIX}")
        )
        if (
            destination_folder.resolve() == source.resolve()
            or source.resolve() in destination_folder.resolve().parents
        ):
            exit_with_error("Choose an output folder outside the input folder.")

        files = recursively_find_supported_media_files(folder_path=source)
        if not files:
            print_error(f"No supported media files were found in {source}.")
            continue

        for item in files:
            relative_path = item.relative_to(source)
            destination_path = destination_folder / generate_default_output_path(
                relative_path
            )
            jobs.append((item, destination_path))

    destinations = [destination.resolve() for _, destination in jobs]
    if len(destinations) != len(set(destinations)):
        exit_with_error(
            "Multiple inputs have the same output filename. "
            "Choose a different output folder."
        )
    return jobs


# --------------------------------------------------
# CLI Entry Point & Dispatch
# --------------------------------------------------


def main() -> None:
    """Parse CLI arguments and dispatch remuxing tasks."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Remux media into QuickTime-compatible MP4 files"
    )
    parser.add_argument(
        "sources",
        nargs="+",
        type=Path,
        help="Media files or folders to process.",
    )
    parser.add_argument(
        "-o", "--output", type=Path, help="Optional output file or folder."
    )
    args: argparse.Namespace = parser.parse_args()

    started: float = time.monotonic()
    print_script_intro("quickmp4", "Remux media into QuickTime-compatible MP4 files")

    try:
        jobs = plan_jobs(args.sources, args.output)
        if not jobs:
            exit_with_error("No media files were found to process.")

        for number, (source, destination) in enumerate(jobs, 1):
            if len(jobs) == 1:
                print_found_item(source, blank_after=False)
            else:
                print()
                print_batch_item(number, len(jobs), source)

            if len(jobs) > 1 and destination.exists():
                print_skipped(destination)
                continue

            remux_single_media_file(source_path=source, output_path=destination)
            print_saved(destination)

        print_elapsed(time.monotonic() - started)
    except KeyboardInterrupt:
        print_canceled()
        sys.exit(130)


if __name__ == "__main__":
    main()
