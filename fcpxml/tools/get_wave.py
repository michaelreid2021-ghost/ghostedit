#!/usr/bin/env python3
# 41_proxy_audio_export.py
# Version: 1.1.0
# Last Modified: 2025-09-04
# Purpose: Extract mono 16kHz WAV audio from proxy video files for the active project,
#          saving to a user-specified directory. Supports start/duration windowing and
#          records clip length (seconds) for each item.

import os
import json
import argparse
import subprocess
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

from project_paths import add_base_arg, from_args  # Assumed available in project

# ---------- Utilities ----------

def run_subprocess(cmd: List[str]) -> Tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr)."""
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False
    )
    return proc.returncode, proc.stdout, proc.stderr


def ensure_fftool_exists(tool_path: str, default_name: str) -> Optional[str]:
    """
    Validate existence of ffmpeg/ffprobe.
    - If a path is provided and exists, use it.
    - If tool_path equals the default_name, let PATH resolution handle it.
    - Otherwise return None to indicate a hard failure.
    """
    p = Path(tool_path)
    if p.exists():
        return str(p)
    if tool_path.lower() == default_name:
        # Allow PATH resolution at runtime
        return default_name
    return None


def get_media_duration(ffprobe_path: str, media_path: Path) -> Optional[float]:
    """
    Get duration in seconds (float) via ffprobe.
    Returns None on failure.
    """
    # Try audio stream duration
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(media_path)
    ]
    code, out, _ = run_subprocess(cmd)
    if code == 0:
        try:
            return float(out.strip())
        except (ValueError, AttributeError):
            pass
    # Fallback: overall container duration
    cmd = [
        ffprobe_path,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(media_path)
    ]
    code, out, _ = run_subprocess(cmd)
    if code == 0:
        try:
            return float(out.strip())
        except (ValueError, AttributeError):
            return None
    return None


def extract_wav(
    ffmpeg_path: str,
    video_path: Path,
    output_wav_path: Path,
    overwrite: bool = True,
    start: Optional[float] = None,      # seconds from start
    duration: Optional[float] = None    # length to extract
) -> None:
    """
    Extract mono 16kHz PCM WAV from (optionally) a time window of the video.
    Places -ss before -i for fast seeking on proxies; adjust if exact-frame accuracy is required.
    """
    cmd: List[str] = [ffmpeg_path]
    cmd.append("-y" if overwrite else "-n")
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(video_path), "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1"]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd.append(str(output_wav_path))
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def find_proxy_for_scene(proxies_dir: Path, proxy_basename: str) -> Optional[Path]:
    """
    Locate proxy file for a scene, preferring .mp4 then .mov.
    """
    mp4 = proxies_dir / f"{proxy_basename}.mp4"
    if mp4.exists():
        return mp4
    mov = proxies_dir / f"{proxy_basename}.mov"
    if mov.exists():
        return mov
    return None


# ---------- Main ----------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export WAV audio from active project's proxy videos and record clip lengths."
    )
    add_base_arg(parser)
    parser.add_argument(
        "--proxies",
        type=Path,
        default=None,
        help="Directory containing proxy .mp4 or .mov files. Defaults to project_paths p.proxies."
    )
    parser.add_argument(
        "--ffmpeg",
        type=str,
        default="ffmpeg",
        help="Path to ffmpeg executable (default: resolve via PATH)."
    )
    parser.add_argument(
        "--ffprobe",
        type=str,
        default="ffprobe",
        help="Path to ffprobe executable (default: resolve via PATH)."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to master_manifest.json (expects scenes with 'scene_number' and 'proxy_filename')."
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=None,
        help="Directory to write extracted WAV files. Defaults to <project_base>/wav_exports."
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=None,
        help="Path to write JSON summary (durations, filenames). Defaults to <outdir>/audio_export_summary.json."
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default="scene",
        help="Filename prefix for WAV outputs (default: 'scene')."
    )
    parser.add_argument(
        "--start",
        type=float,
        default=None,
        help="Start offset in seconds for extraction (e.g., 2.5). If omitted, starts at 0."
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Duration in seconds to extract (e.g., 10). If omitted, exports to end."
    )
    parser.add_argument(
        "--dry_run",
        action="store_true",
        help="List actions without extracting audio."
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Resolve project paths
    p = from_args(args)
    proxies_dir = args.proxies or p.proxies
    manifest_path = args.manifest or p.master_manifest
    outdir = args.outdir or (Path(p.base) / "wav_exports")
    summary_path = args.summary or (outdir / "audio_export_summary.json")

    # Tools
    ffmpeg_path = ensure_fftool_exists(args.ffmpeg, "ffmpeg")
    ffprobe_path = ensure_fftool_exists(args.ffprobe, "ffprobe")

    print("[+] Audio Export Configuration")
    print(f"    base:        {p.base}")
    print(f"    proxies:     {proxies_dir}")
    print(f"    manifest:    {manifest_path}")
    print(f"    outdir:      {outdir}")
    print(f"    summary:     {summary_path}")
    print(f"    ffmpeg:      {ffmpeg_path or 'NOT FOUND'}")
    print(f"    ffprobe:     {ffprobe_path or 'NOT FOUND'}")
    print(f"    start:       {args.start if args.start is not None else 0}")
    print(f"    duration:    {args.duration if args.duration is not None else 'FULL'}")
    print(f"    dry_run:     {args.dry_run}")

    # Validate tools
    if ffmpeg_path is None:
        print("[!] ffmpeg not found at provided path and not using PATH — aborting.")
        return
    if ffprobe_path is None:
        print("[!] ffprobe not found at provided path and not using PATH — aborting.")
        return

    # Validate inputs
    if not Path(proxies_dir).exists():
        print(f"[!] Proxies directory not found: {proxies_dir}")
        return
    if not Path(manifest_path).exists():
        print(f"[!] Master manifest not found: {manifest_path}")
        return

    # Load scenes
    try:
        scenes: List[Dict[str, Any]] = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[!] Failed to read/parse manifest: {type(e).__name__}: {e}")
        return

    print(f"[+] {len(scenes)} scenes found in manifest.")
    outdir.mkdir(parents=True, exist_ok=True)

    summary_records: List[Dict[str, Any]] = []

    for i, scene in enumerate(scenes, start=1):
        proxy_basename = Path(scene.get("proxy_filename", "")).stem
        scene_number = scene.get("scene_number", i)

        print(f"({i}/{len(scenes)}) Processing scene {scene_number} :: {proxy_basename}")

        if not proxy_basename:
            print("    [!] Missing proxy_filename in scene; skipping.")
            summary_records.append({
                "scene_number": scene_number,
                "proxy_basename": None,
                "status": "missing_proxy_filename"
            })
            continue

        proxy_video = find_proxy_for_scene(Path(proxies_dir), proxy_basename)
        if proxy_video is None:
            print(f"    [!] Proxy not found (.mp4/.mov) for '{proxy_basename}'; skipping.")
            summary_records.append({
                "scene_number": scene_number,
                "proxy_basename": proxy_basename,
                "status": "proxy_missing"
            })
            continue

        # Determine full duration
        full_duration_sec = get_media_duration(ffprobe_path, proxy_video)

        # Compute extracted window duration if possible
        extracted_duration_sec: Optional[float] = None
        if args.duration is not None:
            if full_duration_sec is not None:
                start_offset = args.start or 0.0
                remaining = max(0.0, full_duration_sec - start_offset)
                extracted_duration_sec = min(remaining, args.duration)
            else:
                extracted_duration_sec = args.duration

        # Output WAV filename
        wav_name_parts = [f"{args.prefix}_{scene_number:04d}_{proxy_video.stem}"]
        if args.start is not None:
            wav_name_parts.append(f"ss{int(args.start)}")
        if args.duration is not None:
            # keep integer-ish if close, else keep float
            dur_str = str(int(args.duration)) if abs(args.duration - int(args.duration)) < 1e-6 else str(args.duration)
            wav_name_parts.append(f"t{dur_str}")
        wav_name = "_".join(wav_name_parts) + ".wav"
        wav_path = Path(outdir) / wav_name

        # Extract or simulate
        if args.dry_run:
            if extracted_duration_sec is not None:
                print(f"    -> DRY RUN: Would extract to {wav_path} (window ~ {extracted_duration_sec:.3f}s)")
            else:
                print(f"    -> DRY RUN: Would extract to {wav_path} (full or unknown duration)")
            status = "dry_run"
        else:
            try:
                extract_wav(
                    ffmpeg_path=ffmpeg_path,
                    video_path=proxy_video,
                    output_wav_path=wav_path,
                    overwrite=True,
                    start=args.start,
                    duration=args.duration
                )
                print(f"    -> Wrote: {wav_path.name}")
                status = "ok"
            except subprocess.CalledProcessError as e:
                print(f"    [!] ffmpeg extraction failed: {e}")
                status = "ffmpeg_error"
            except Exception as e:
                print(f"    [!] Unexpected error: {type(e).__name__}: {e}")
                status = "error"

        # Record summary entry
        rec: Dict[str, Any] = {
            "scene_number": scene_number,
            "proxy_file": str(proxy_video),
            "output_wav": str(wav_path),
            "duration_seconds_full": full_duration_sec,
            "extracted_duration_seconds": extracted_duration_sec,
            "start_offset_seconds": args.start or 0.0,
            "status": status
        }
        # Preserve select original scene metadata when available
        for k in ("start_time", "end_time", "transcript", "timed_transcript"):
            if k in scene:
                rec[k] = scene[k]
        summary_records.append(rec)

    # Write summary JSON
    try:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary_records, indent=2), encoding="utf-8")
        print(f"\n[+] Summary written: {summary_path}")
    except Exception as e:
        print(f"[!] Failed to write summary: {type(e).__name__}: {e}")

    print("[+] Done.")


if __name__ == "__main__":
    main()

# Changelog:
# 1.1.0 (2025-09-04): Add --start and --duration CLI options; filenames reflect window; summary records extracted window.
# 1.0.0 (2025-09-04): Initial version derived from transcription script; replaces Whisper with pure audio export.
#                     Adds clip length via ffprobe, per-scene WAV export, and JSON summary manifest.
