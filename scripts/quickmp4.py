"""Remux media files into QuickTime-compatible MP4 containers using GPAC.

Remuxes video, audio, and subtitles into standard MP4 containers with
appropriate FourCC tags and metadata for Apple QuickTime and AVFoundation.

Specifications & Stream Handling:
    - Video:
        - Stream: Passthrough copy with AVC/HEVC reframing (rfnalu)
        - Parameter sets: Out-of-band sample descriptions (xps_inband=no)
        - FourCC tags: avc1 (H.264), hvc1 (HEVC), av01 (AV1), dvh1/dva1 (Dolby Vision)
        - Sync table: All SAP types indexed (forcesync)
    - Audio:
        - Passthrough codecs: AAC, Apple Lossless (ALAC), AC-3, E-AC-3, MP3, FLAC
        - Fallback codec: Apple Lossless (ALAC via c=alac)
    - Subtitles:
        - Text subtitles converted to QuickTime Timed Text (tx3g / mov_text)
    - Container:
        - Brand: mp42 (styp=mp42)
        - Flags: Fast start (store=fstart)
        - Chapters: Timed text track and user-data (chapm=both)
        - Tags: Apple iTunes format (itags=strict)
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
from typing import Any, Final, NoReturn

# ----------------------------------------------------------------------
# Constants & Configuration
# ----------------------------------------------------------------------

VERSION: Final[int] = 1
TEMP_DIRECTORY_PREFIX: Final[str] = ".quickmp4-"
FOLDER_OUTPUT_SUFFIX: Final[str] = ".quickmp4"
FFPROBE_TIMEOUT_SECONDS: Final[int] = 60

SUPPORTED_MEDIA_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".mkv",
    ".mp4",
    ".m4v",
    ".mov",
    ".webm",
    ".avi",
})

COMPATIBLE_AUDIO_CODECS: Final[frozenset[str]] = frozenset({
    "aac",
    "alac",
    "ac3",
    "eac3",
    "mp3",
    "flac",
})

# ANSI Terminal Color Codes
ANSI_BOLD: Final[str] = "\033[1m"
ANSI_DIM: Final[str] = "\033[2m"
ANSI_GREEN: Final[str] = "\033[32m"
ANSI_CYAN_BOLD: Final[str] = "\033[1;36m"
ANSI_YELLOW: Final[str] = "\033[33m"
ANSI_RED: Final[str] = "\033[31m"
ANSI_RESET: Final[str] = "\033[0m"


# ----------------------------------------------------------------------
# Terminal Output Helpers
# ----------------------------------------------------------------------


def exit_with_error(message: str, exit_code: int = 1) -> NoReturn:
    """Print a formatted error message to standard error and terminate."""
    sys.stderr.write(f"{ANSI_RED}Error:{ANSI_RESET} {message}\n")
    sys.exit(exit_code)


def format_elapsed_time(seconds: float) -> str:
    """Format duration in seconds to a human-readable string (e.g., '1h 2m 3s')."""
    total_seconds: int = max(0, round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)

    time_components: list[str] = []
    if hours > 0:
        time_components.append(f"{hours}h")
    if minutes > 0 or hours > 0:
        time_components.append(f"{minutes}m")
    time_components.append(f"{remaining_seconds}s")

    return " ".join(time_components)


# ----------------------------------------------------------------------
# Step 1: Media Inspection (ffprobe)
# ----------------------------------------------------------------------


def find_required_tool(binary_name: str) -> str:
    """Verify that an external binary exists in the system PATH."""
    binary_path: str | None = shutil.which(binary_name)
    if not binary_path:
        exit_with_error(f"The required tool '{binary_name}' was not found in PATH.")
    return binary_path


def probe_media_streams(source_file_path: Path) -> list[dict[str, Any]]:
    """Inspect stream metadata from a media file using ffprobe."""
    ffprobe_executable: str = find_required_tool("ffprobe")

    probe_command: list[str] = [
        ffprobe_executable,
        "-v",
        "error",
        "-show_entries",
        (
            "stream=index,codec_type,codec_name,r_frame_rate,avg_frame_rate:"
            "stream_disposition:"
            "stream_side_data=side_data_type,dv_profile"
        ),
        "-of",
        "json",
        str(source_file_path),
    ]

    try:
        process_result: subprocess.CompletedProcess[str] = subprocess.run(
            probe_command,
            capture_output=True,
            text=True,
            check=False,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        exit_with_error(
            f"ffprobe didn't respond within {FFPROBE_TIMEOUT_SECONDS} seconds "
            f"for '{source_file_path.name}'."
        )

    if process_result.returncode != 0:
        error_output: str = process_result.stderr.strip()
        exit_with_error(
            f"Unable to inspect '{source_file_path.name}' with ffprobe:\n{error_output}"
        )

    try:
        parsed_json: dict[str, Any] = json.loads(process_result.stdout)
        return parsed_json.get("streams", [])
    except json.JSONDecodeError:
        exit_with_error(
            f"Unable to parse ffprobe JSON output for '{source_file_path.name}'."
        )


def extract_frame_rate(video_stream: dict[str, Any]) -> str | None:
    """Extract a valid rational frame rate string from video stream metadata."""
    rate_key: str
    for rate_key in ("r_frame_rate", "avg_frame_rate"):
        rate_value: str = str(video_stream.get(rate_key, "")).strip()
        if rate_value and rate_value != "0/0" and "/" in rate_value:
            numerator: str
            denominator: str
            numerator, _, denominator = rate_value.partition("/")
            if numerator.isdigit() and denominator.isdigit() and int(denominator) > 0:
                return rate_value
    return None


def extract_dolby_vision_profile(video_stream: dict[str, Any]) -> int | None:
    """Extract Dolby Vision profile number if present in stream side data."""
    side_data_entries: list[dict[str, Any]] = video_stream.get("side_data_list", [])
    entry: dict[str, Any]
    for entry in side_data_entries:
        if entry.get("side_data_type") == "DOVI configuration record":
            return entry.get("dv_profile")
    return None


def find_primary_video_stream(
    streams: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """Locate the primary video stream, ignoring attached pictures / cover art."""
    stream: dict[str, Any]
    for stream in streams:
        if stream.get("codec_type") == "video" and not stream.get(
            "disposition", {}
        ).get("attached_pic"):
            return stream
    return None


# ----------------------------------------------------------------------
# Step 2: Build GPAC Remux Command
# ----------------------------------------------------------------------


def build_gpac_command(
    source_file_path: Path,
    output_file_path: Path,
    streams: Sequence[dict[str, Any]],
    video_stream: dict[str, Any],
    verbose: bool = False,
) -> list[str]:
    """Construct the complete GPAC command for Apple QuickTime compatible MP4 muxing."""
    gpac_executable: str = find_required_tool("gpac")

    command: list[str] = [gpac_executable]
    if not verbose:
        command.append("-logs=all@error")

    # Pass the source file with source FPS property to avoid timing drift
    source_frame_rate: str | None = extract_frame_rate(video_stream)
    source_fps_property: str = f":#FPS={source_frame_rate}" if source_frame_rate else ""
    command.extend(["-i", f"{source_file_path}{source_fps_property}"])

    # 1. Video Reframing (AVC / HEVC)
    video_codec_name: str = str(video_stream.get("codec_name", "")).lower()
    if video_codec_name in {"h264", "hevc"}:
        video_filter_options: list[str] = []

        if source_frame_rate:
            video_filter_options.append(f"fps={source_frame_rate}")

        dolby_vision_profile: int | None = extract_dolby_vision_profile(video_stream)
        # Profile 5 (HEVC) and Profile 9 (AVC) use dedicated DV sample entries (dvh1/dva1).
        # Profile 8 and 7 have HDR10/SDR cross-compatible base layers and must use standard
        # hvc1 sample entries for QuickTime / Apple AVFoundation compatibility.
        if dolby_vision_profile is not None and dolby_vision_profile not in {
            5,
            9,
        }:
            video_filter_options.append("dv_mode=none")

        # rfnalu reframes raw NAL units into ISOBMFF samples, creates avcC/hvcC boxes,
        # and removes in-band Access Unit Delimiters (AUD).
        # Note: Do not prefix with '@#video'; in GPAC's implicit chaining mode, explicit links
        # cause duplicate un-reframed video tracks.
        if video_filter_options:
            options_string: str = ":".join(video_filter_options)
            command.append(f"rfnalu:{options_string}")

    # 2. Audio Processing (Passthrough compatible formats, fallback transcode to ALAC)
    audio_streams: list[dict[str, Any]] = [
        stream for stream in streams if stream.get("codec_type") == "audio"
    ]
    if audio_streams:
        fallback_audio_codec: str = "alac"
        needs_audio_transcode: bool = any(
            str(stream.get("codec_name", "")).lower() not in COMPATIBLE_AUDIO_CODECS
            for stream in audio_streams
        )

        if needs_audio_transcode:
            # ccp (Codec Check Passthrough) copies compatible formats (AAC, AC-3,
            # E-AC-3, FLAC, MP3, ALAC) while transcoding incompatible formats
            # (DTS, TrueHD, Opus) to ALAC, so already-compatible tracks are
            # never wastefully re-encoded alac -> alac.
            passthrough_codecs_string: str = ",".join(sorted(COMPATIBLE_AUDIO_CODECS))
            command.append(f"c={fallback_audio_codec}:ccp={passthrough_codecs_string}")

    # 3. Output MP4 Multiplexer Configuration (mp4mx)
    # - store=fstart: Fast-start mode (moov atom before mdat) for instant playback
    # - xps_inband=no: Stores SPS/PPS/VPS out-of-band in avcC/hvcC boxes for Apple VideoToolbox
    # - forcesync: Indexes all Stream Access Points in the stss sync table for smooth seeking
    # - styp=mp42: Sets major container brand to standard MPEG-4 Version 2
    # - chapm=both: Writes chapters as both a QuickTime text track and user-data (chpl) atom
    # - itags=strict: Filters tags to recognized Apple iTunes metadata keys
    # - ase=v0: Standard v0 audio sample entry signaling for multichannel compatibility
    multiplexer_options: str = (
        "store=fstart:xps_inband=no:forcesync:styp=mp42:chapm=both:itags=strict:ase=v0"
    )
    command.extend([f"mp4mx:{multiplexer_options}", "-o", str(output_file_path)])

    return command


# ----------------------------------------------------------------------
# Step 3: Atomic File Remux Execution
# ----------------------------------------------------------------------


def remux_file(
    source_file_path: Path, output_file_path: Path, verbose: bool = False
) -> None:
    """Remux a single media file to MP4 using an atomic temporary directory write."""
    if not source_file_path.is_file():
        exit_with_error(f"Input file not found: {source_file_path}")

    output_file_path.parent.mkdir(parents=True, exist_ok=True)
    stream_metadata: list[dict[str, Any]] = probe_media_streams(source_file_path)
    video_stream: dict[str, Any] | None = find_primary_video_stream(stream_metadata)
    if video_stream is None:
        exit_with_error(
            f"No valid video stream was found in '{source_file_path.name}'."
        )

    # Use a temporary directory adjacent to the destination to ensure atomic rename
    with tempfile.TemporaryDirectory(
        prefix=TEMP_DIRECTORY_PREFIX, dir=output_file_path.parent
    ) as temporary_directory:
        temporary_output_path: Path = Path(temporary_directory) / output_file_path.name
        gpac_command: list[str] = build_gpac_command(
            source_file_path,
            temporary_output_path,
            stream_metadata,
            video_stream,
            verbose,
        )

        print(shlex.join(gpac_command))
        process_result: subprocess.CompletedProcess[bytes] = subprocess.run(
            gpac_command, check=False
        )

        if process_result.returncode != 0:
            exit_with_error(
                f"Unable to remux '{source_file_path.name}' with GPAC "
                f"(exit code {process_result.returncode})."
            )

        if (
            not temporary_output_path.is_file()
            or temporary_output_path.stat().st_size == 0
        ):
            exit_with_error(
                f"GPAC didn't produce an output file for '{source_file_path.name}'."
            )

        os.replace(temporary_output_path, output_file_path)


# ----------------------------------------------------------------------
# Step 4: Batch & Path Planning
# ----------------------------------------------------------------------


def mp4_output_path_for(source_path: Path) -> Path:
    """Generate output path with .mp4 or .quicktime.mp4 extension."""
    target_suffix: str = (
        ".quicktime.mp4" if source_path.suffix.lower() == ".mp4" else ".mp4"
    )
    return source_path.with_suffix(target_suffix)


def collect_media_files_from_directory(directory_path: Path) -> list[Path]:
    """Recursively collect supported media files from a directory."""
    return sorted(
        path
        for path in directory_path.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS
    )


def resolve_file_job(
    source_file: Path,
    output_destination: Path | None,
    has_multiple_inputs: bool,
) -> tuple[Path, Path]:
    """Resolve destination path for a single input file."""
    if output_destination is None:
        return source_file, mp4_output_path_for(source_file)

    if has_multiple_inputs:
        relative_target: Path = mp4_output_path_for(Path(source_file.name))
        return source_file, output_destination / relative_target

    return source_file, output_destination


def resolve_folder_jobs(
    source_folder: Path,
    output_destination: Path | None,
    has_multiple_inputs: bool,
) -> list[tuple[Path, Path]]:
    """Resolve destination paths for all media files inside a directory."""
    if not source_folder.is_dir():
        exit_with_error(f"Input is not a valid file or folder: {source_folder}")

    destination_folder: Path = (
        output_destination / source_folder.name
        if output_destination and has_multiple_inputs
        else output_destination
        or source_folder.with_name(f"{source_folder.name}{FOLDER_OUTPUT_SUFFIX}")
    )

    resolved_destination: Path = destination_folder.resolve()
    resolved_source: Path = source_folder.resolve()
    if (
        resolved_destination == resolved_source
        or resolved_source in resolved_destination.parents
    ):
        exit_with_error("Output folder cannot be inside the input folder.")

    discovered_files: list[Path] = collect_media_files_from_directory(source_folder)
    if not discovered_files:
        exit_with_error(f"No supported media files were found in {source_folder}.")

    folder_jobs: list[tuple[Path, Path]] = []
    media_file: Path
    for media_file in discovered_files:
        relative_path: Path = media_file.relative_to(source_folder)
        target_path: Path = destination_folder / mp4_output_path_for(relative_path)
        folder_jobs.append((media_file, target_path))

    return folder_jobs


def plan_remux_jobs(
    sources: Sequence[Path], output_destination: Path | None
) -> list[tuple[Path, Path]]:
    """Determine source-to-destination pairs for all requested files and folders."""
    remux_jobs: list[tuple[Path, Path]] = []
    has_multiple_inputs: bool = len(sources) > 1

    source: Path
    for source in sources:
        if source.is_file():
            remux_jobs.append(
                resolve_file_job(source, output_destination, has_multiple_inputs)
            )
        elif source.is_dir():
            remux_jobs.extend(
                resolve_folder_jobs(source, output_destination, has_multiple_inputs)
            )
        else:
            exit_with_error(f"Input is not a valid file or folder: {source}")

    # Guard against accidental filename collisions in batch destinations
    all_destinations: list[Path] = [
        target_path.resolve() for _, target_path in remux_jobs
    ]
    if len(all_destinations) != len(set(all_destinations)):
        exit_with_error(
            "Multiple inputs resolve to the same output file. Choose a unique output folder."
        )

    return remux_jobs


# ----------------------------------------------------------------------
# Step 5: Main Entry Point
# ----------------------------------------------------------------------


def main() -> None:
    """Parse command line arguments and execute the remuxing pipeline."""
    argument_parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Remux media into QuickTime-compatible MP4 files using GPAC"
    )
    argument_parser.add_argument(
        "sources",
        nargs="+",
        type=Path,
        help="Media files or folders to process.",
    )
    argument_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Optional output file or folder destination.",
    )
    argument_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show full GPAC log output and warnings.",
    )
    arguments: argparse.Namespace = argument_parser.parse_args()

    start_time: float = time.monotonic()
    print(
        f"\n{ANSI_BOLD}quickmp4 v{VERSION}{ANSI_RESET}: "
        "Remux media into QuickTime-compatible MP4 files using GPAC"
    )

    try:
        remux_jobs: list[tuple[Path, Path]] = plan_remux_jobs(
            arguments.sources, arguments.output
        )

        total_jobs: int = len(remux_jobs)
        index: int
        source_file: Path
        target_file: Path
        for index, (source_file, target_file) in enumerate(remux_jobs, 1):
            batch_prefix: str = (
                f"{ANSI_BOLD}({index}/{total_jobs}){ANSI_RESET} "
                if total_jobs > 1
                else ""
            )
            print(
                f"\n{batch_prefix}Found [{ANSI_CYAN_BOLD}{source_file.name}{ANSI_RESET}]"
            )

            # Guard clause: skip existing files in batch mode
            if total_jobs > 1 and target_file.exists():
                print(f"{ANSI_YELLOW}Skipped{ANSI_RESET} [{target_file}]")
                continue

            # Single-file mode replaces an existing destination on purpose.
            if total_jobs == 1 and target_file.exists():
                print(f"{ANSI_YELLOW}Overwriting{ANSI_RESET} [{target_file}]")

            remux_file(source_file, target_file, arguments.verbose)
            print(f"{ANSI_GREEN}Saved {target_file}{ANSI_RESET}")

        formatted_duration: str = format_elapsed_time(time.monotonic() - start_time)
        print(f"{ANSI_DIM}Completed in {formatted_duration}{ANSI_RESET}")

    except KeyboardInterrupt:
        sys.stderr.write(
            f"{ANSI_YELLOW}\nCanceled. Incomplete files were removed.{ANSI_RESET}\n"
        )
        sys.exit(130)


if __name__ == "__main__":
    main()
