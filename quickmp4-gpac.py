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
        - Passthrough codecs: AAC, Apple Lossless (ALAC), AC-3, E-AC-3, MP3
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
from typing import Final, NoReturn, TypedDict

# --------------------------------------------------
# Global Configuration
# --------------------------------------------------

VERSION: Final[int] = 1

COPY_AUDIO_CODECS: Final[frozenset[str]] = frozenset({
    "aac",
    "alac",
    "ac3",
    "eac3",
    "mp3",
})
FALLBACK_AUDIO_CODEC: Final[str] = "alac"
TEMP_DIR_PREFIX: Final[str] = ".quickmp4-gpac-"
RE_REMUX_MP4_SUFFIX: Final[str] = ".quicktime.mp4"
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
    r_frame_rate: str
    avg_frame_rate: str
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
            "stream=index,codec_type,codec_name,r_frame_rate,avg_frame_rate:"
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
# Path Resolution Helpers
# --------------------------------------------------


def generate_default_output_path(source_path: Path) -> Path:
    """Generate default output path with .mp4 extension."""
    suffix: str = (
        RE_REMUX_MP4_SUFFIX if source_path.suffix.lower() == ".mp4" else ".mp4"
    )
    return source_path.with_suffix(suffix)


def recursively_find_supported_media_files(folder_path: Path) -> list[Path]:
    """Recursively collect supported media files from a folder."""
    media_extensions: Final[frozenset[str]] = frozenset({
        ".mkv",
        ".mp4",
        ".m4v",
        ".mov",
        ".webm",
        ".avi",
    })
    return sorted(
        path
        for path in folder_path.rglob(pattern="*")
        if path.is_file() and path.suffix.lower() in media_extensions
    )


# --------------------------------------------------
# Build GPAC Command
# --------------------------------------------------


def extract_video_fps(video_stream: Stream) -> str | None:
    """Extract a valid rational frame rate string from stream metadata."""
    for key in ("r_frame_rate", "avg_frame_rate"):
        rate: str = str(object=video_stream.get(key, "")).strip()  # type: ignore[literal-required]
        if rate and rate != "0/0" and "/" in rate:
            num, _, den = rate.partition("/")
            if num.isdigit() and den.isdigit() and int(den) > 0:
                return rate
    return None


def extract_dolby_vision_profile(video_stream: Stream) -> int | None:
    """Extract Dolby Vision profile if present."""
    side_data_list: list[SideData] = video_stream.get("side_data_list", [])
    return next(
        (
            item.get("dv_profile")
            for item in side_data_list
            if item.get("side_data_type") == "DOVI configuration record"
        ),
        None,
    )


def construct_gpac_remux_command(
    source_path: Path,
    output_path: Path,
    streams: Sequence[Stream],
    verbose: bool = False,
) -> list[str]:
    """Build the GPAC command-line arguments for Apple QuickTime compatible remuxing."""
    gpac: str = validate_required_binary_in_path(binary_name="gpac")

    # Locate main video stream, ignoring attached cover art pictures
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

    fps: str | None = extract_video_fps(video_stream=video)
    # Signal source frame rate to the input demuxer to avoid timing drift
    fps_prop: str = f":#FPS={fps}" if fps else ""

    command: list[str] = [gpac]
    if not verbose:
        command.append("-logs=all@error")
    command.extend(["-i", f"{source_path}{fps_prop}"])

    video_codec: str = str(object=video.get("codec_name", "")).lower()
    if video_codec in {"h264", "hevc"}:
        rf_opts: list[str] = []
        if fps:
            rf_opts.append(f"fps={fps}")

        dv_profile: int | None = extract_dolby_vision_profile(video_stream=video)
        # Profile 5 (HEVC) and Profile 9 (AVC) use dedicated DV sample entries (dvh1/dva1).
        # Profile 8 and 7 have HDR10/SDR cross-compatible base layers and must use standard
        # hvc1 sample entries for QuickTime / Apple AVFoundation compatibility.
        if dv_profile is not None and dv_profile not in {5, 9}:
            rf_opts.append("dv_mode=none")

        # rfnalu reframes raw NAL units into ISOBMFF samples, creates avcC/hvcC boxes,
        # and removes in-band Access Unit Delimiters (AUD).
        # CRITICAL: Do NOT prefix with explicit link directives like '@#video'.
        # In GPAC's implicit chaining mode, an explicit '@#video' creates a dedicated branch
        # while leaving the source video PID unconsumed, which causes GPAC to also route the
        # raw un-reframed video directly to mp4mx, resulting in duplicate video tracks.
        if rf_opts:
            rf_opts_str: str = f":{':'.join(rf_opts)}"
            command.append(f"rfnalu{rf_opts_str}")
    # Note: For AV1, GPAC mp4mx handles av01 sample formatting automatically.

    # Audio stream processing:
    # Uses GPAC's cloneable encoder filter syntax (c=alac:ccp=...).
    # ccp (Codec Check Passthrough) replaces the encoder with a passthrough reframer
    # for compatible codecs (AAC, ALAC, AC-3, E-AC-3, MP3), while transcoding
    # incompatible codecs (FLAC, DTS, TrueHD, Opus, PCM) to Apple Lossless (ALAC).
    #
    # CRITICAL: Do NOT prefix with '@#audio'.
    # Setting an explicit '@#audio' link constraint prevents GPAC's intermediate decoder
    # filter (ffdec) from connecting to the encoder (ffenc), causing 'Filters not connected: ffenc'
    # and falling back to muxing unplayable raw DTS/FLAC bitstreams.
    audio_streams: list[Stream] = [
        stream for stream in streams if stream.get("codec_type") == "audio"
    ]
    if audio_streams:
        needs_audio_transcode: bool = any(
            str(object=stream.get("codec_name", "")).lower() not in COPY_AUDIO_CODECS
            for stream in audio_streams
        )
        if needs_audio_transcode:
            ccp_codecs: str = ",".join(
                sorted(c for c in COPY_AUDIO_CODECS if c != FALLBACK_AUDIO_CODEC)
            )
            command.append(f"c={FALLBACK_AUDIO_CODEC}:ccp={ccp_codecs}")

    # Note: GPAC demuxer converts embedded text subtitles (SRT, ASS, WebVTT)
    # directly to QuickTime Timed Text (tx3g) for the MP4 multiplexer.

    # MP4 Output Multiplexer (mp4mx) configuration flags:
    # - store=fstart: Fast-start mode, writes moov atom before mdat for instant playback
    # - xps_inband=no: Stores SPS/PPS/VPS out-of-band in avcC/hvcC boxes for VideoToolbox
    # - forcesync: Indexes all Stream Access Points (including Open-GOP / CRA keyframes)
    #   in the stss sync table so QuickTime Player can seek across the file
    # - styp=mp42: Sets the container major brand to mp42 for standard MPEG-4 Version 2
    # - chapm=both: Emits chapters as both a QuickTime text track and udta.chpl user-data atom
    # - itags=strict: Filters metadata tags to recognized Apple iTunes metadata keys
    # - ase=v0: Uses standard v0 audio sample entry signaling for multichannel compatibility
    #
    # CRITICAL MULTIPLEXER PLACEMENT:
    # 'mp4mx:<opts>' MUST be declared explicitly as a discrete filter before '-o <path>'.
    # If options are attached directly to '-o <path>:<opts>', GPAC instantiates 'fout:dst=...',
    # and when multiple subtitle tracks are converted via txtin, GPAC spawns a separate mp4mx+fout
    # instance for EACH subtitle track. Multiple fout sinks then write to the same output file
    # simultaneously, corrupting the HEVC bitstream, dropping subtitle tracks, and breaking menus.
    mp4_brand: Final[str] = "mp42"
    mp4mx_opts: str = (
        f"store=fstart:xps_inband=no:forcesync:styp={mp4_brand}:"
        "chapm=both:itags=strict:ase=v0"
    )
    command.extend([f"mp4mx:{mp4mx_opts}", "-o", str(output_path)])
    return command


# --------------------------------------------------
# Remux Execution & Job Planning
# --------------------------------------------------


def remux_single_media_file(
    source_path: Path, output_path: Path, verbose: bool = False
) -> None:
    """Remux a single media file to MP4 using an atomic temporary write."""
    if not source_path.is_file():
        exit_with_error(f"The input file wasn't found: {source_path}.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    streams: list[Stream] = probe_media_streams_with_ffprobe(source_path=source_path)

    with tempfile.TemporaryDirectory(
        prefix=TEMP_DIR_PREFIX, dir=output_path.parent
    ) as temp_dir:
        temp_output: Path = Path(temp_dir) / output_path.name
        command: list[str] = construct_gpac_remux_command(
            source_path=source_path,
            output_path=temp_output,
            streams=streams,
            verbose=verbose,
        )
        print(shlex.join(command))

        result: subprocess.CompletedProcess[bytes] = subprocess.run(
            args=command, check=False
        )
        if result.returncode != 0:
            exit_with_error(
                f"Unable to remux '{source_path.name}' with GPAC (exit code {result.returncode})."
            )

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
        description="Remux media into QuickTime-compatible MP4 files using GPAC"
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
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show full GPAC log output and warnings.",
    )
    args: argparse.Namespace = parser.parse_args()

    started: float = time.monotonic()
    print_script_intro(
        "quickmp4-gpac",
        "Remux media into QuickTime-compatible MP4 files using GPAC",
    )

    try:
        jobs = plan_jobs(sources=args.sources, output=args.output)
        if not jobs:
            exit_with_error("No media files were found to process.")

        for number, (source, destination) in enumerate(jobs, 1):
            if len(jobs) == 1:
                print_found_item(path=source, blank_after=False)
            else:
                print()
                print_batch_item(position=number, total=len(jobs), path=source)

            if len(jobs) > 1 and destination.exists():
                print_skipped(path=destination)
                continue

            remux_single_media_file(
                source_path=source,
                output_path=destination,
                verbose=args.verbose,
            )
            print_saved(path=destination)

        print_elapsed(seconds=time.monotonic() - started)
    except KeyboardInterrupt:
        print_canceled()
        sys.exit(130)


if __name__ == "__main__":
    main()
