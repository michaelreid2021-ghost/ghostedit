#!/usr/bin/env python
# -----------------------------------------------------------------------------
# Version: 1.2.0
# Last Modified: 2025-09-03
# Purpose: Resolve Studio automation to generate subtitles via API (AI-capable
#          auto-captioning) and export an SRT by reading the subtitle items.
#
# Notes & API references:
#  - Timeline.CreateSubtitlesFromAudio({autoCaptionSettings}) and its settings
#    keys/enums are documented in the Resolve README (Auto Caption Settings). :contentReference[oaicite:0]{index=0} :contentReference[oaicite:1]{index=1}
#  - The CreateSubtitlesFromAudio method is available on Timeline. :contentReference[oaicite:2]{index=2}
# -----------------------------------------------------------------------------

import os
import sys
import math
from typing import List, Tuple

try:
    # Prefer the helper if present in your environment
    from python_get_resolve import GetResolve  # noqa: F401
    def _get_resolve():
        return GetResolve()
except Exception:
    # Fallback to the official module name if helper isn't available
    import DaVinciResolveScript as dvr_script  # type: ignore
    def _get_resolve():
        return dvr_script.scriptapp("Resolve")


def _fail(msg: str) -> None:
    print(f"Error: {msg}")
    sys.exit(1)


def _frames_to_srt_time(frames: int, fps: float) -> str:
    """Convert timeline frames -> SRT timestamp 'HH:MM:SS,mmm'."""
    if fps <= 0:
        fps = 24.0
    seconds = frames / fps
    millis = int(round((seconds - math.floor(seconds)) * 1000.0))
    total_seconds = int(math.floor(seconds))
    hh = total_seconds // 3600
    mm = (total_seconds % 3600) // 60
    ss = total_seconds % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d},{millis:03d}"


def _detect_fps(project, timeline) -> float:
    """
    Try Timeline setting first, then Project setting, else fallback to Resolve version.
    Returns a float FPS (e.g., 23.976).
    """
    # Timeline setting typically returns a string like "23.976" or "29.97 DF"
    tl_rate = timeline.GetSetting("timelineFrameRate")
    if isinstance(tl_rate, str) and tl_rate.strip():
        val = tl_rate.replace(" DF", "").strip()
        try:
            return float(val)
        except Exception:
            pass

    pr_rate = project.GetSetting("timelineFrameRate")
    if isinstance(pr_rate, str) and pr_rate.strip():
        val = pr_rate.replace(" DF", "").strip()
        try:
            return float(val)
        except Exception:
            pass

    # Fallback
    return 24.0


def _first_nonempty_subtitle_track(timeline):
    st_count = timeline.GetTrackCount("subtitle")
    for idx in range(1, st_count + 1):
        items = timeline.GetItemListInTrack("subtitle", idx)
        if items:
            return idx, items
    return None, []


def _export_srt_from_subtitle_track(timeline, track_index: int, items, fps: float, out_path: str) -> bool:
    # Collect (start, end, text) tuples
    records: List[Tuple[int, int, str]] = []
    for it in items:
        try:
            start_fr = int(round(it.GetStart(False)))
            end_fr = int(round(it.GetEnd(False)))
            # Subtitle text is the timeline item's name for subtitle clips
            text = it.GetName() or ""
            # Resolve sometimes embeds newline formatting; normalize to SRT-friendly lines
            text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
            if end_fr <= start_fr:
                continue
            records.append((start_fr, end_fr, text))
        except Exception:
            continue

    if not records:
        return False

    records.sort(key=lambda r: r[0])

    lines: List[str] = []
    for i, (s, e, tx) in enumerate(records, start=1):
        start_tc = _frames_to_srt_time(s, fps)
        end_tc = _frames_to_srt_time(e, fps)
        # SRT blocks: index, time range, text, blank
        lines.append(str(i))
        lines.append(f"{start_tc} --> {end_tc}")
        # Split multi-line subtitles into separate lines (already normalized)
        lines.extend(tx.split("\n") if tx else [""])
        lines.append("")  # blank line between entries

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return True
    except Exception as ex:
        print(f"Failed writing SRT: {ex}")
        return False


def main():
    resolve = _get_resolve()
    if resolve is None:
        _fail("Cannot connect to DaVinci Resolve scripting API.")

    projectManager = resolve.GetProjectManager()
    project = projectManager.GetCurrentProject()
    if not project:
        _fail("No project is loaded. Open a project and retry.")

    timeline = project.GetCurrentTimeline()
    if not timeline:
        _fail("No active timeline. Open a timeline and retry.")

    print(f"Connected: Resolve v{resolve.GetVersionString()}")
    print(f"Project: '{project.GetName()}'")
    print(f"Timeline: '{timeline.GetName()}'")
    print("-" * 28)

    # --- Pre-flight: ensure we have at least one audio track with clips (helps catch common pitfalls)
    print("--- Checking audio presence ---")
    a_count = timeline.GetTrackCount("audio")
    print(f"Audio tracks: {a_count}")
    if a_count == 0:
        _fail("No audio tracks found on the timeline.")

    total_audio_clips = 0
    for i in range(1, a_count + 1):
        clips = timeline.GetItemListInTrack("audio", i) or []
        print(f"  A{i}: {len(clips)} clip(s)")
        total_audio_clips += len(clips)
    if total_audio_clips == 0:
        _fail("Audio tracks are present but empty; cannot auto-caption.")

    print("-" * 28)
    print("--- Generating subtitles (AI-capable auto-caption) ---")

    # Auto-caption settings: use documented enum keys & values (see README cites above)
    try:
        autoCaptionSettings = {
            resolve.SUBTITLE_LANGUAGE: resolve.AUTO_CAPTION_ENGLISH,
            resolve.SUBTITLE_CAPTION_PRESET: resolve.AUTO_CAPTION_SUBTITLE_DEFAULT,
            resolve.SUBTITLE_CHARS_PER_LINE: 42,
            resolve.SUBTITLE_LINE_BREAK: resolve.AUTO_CAPTION_LINE_SINGLE,
            resolve.SUBTITLE_GAP: 0,
        }
    except AttributeError:
        _fail("Your Resolve API build is missing Auto Caption enums. Update Resolve Studio and its scripting package.")

    ok = timeline.CreateSubtitlesFromAudio(autoCaptionSettings)
    if not ok:
        _fail("CreateSubtitlesFromAudio() failed. Check that dialogue is on an enabled track and not inside a problematic container.")

    print("Subtitles generated on a new subtitle track (or appended to existing).")
    print("-" * 28)

    # --- Export SRT by reading subtitle items from the first non-empty subtitle track
    print("--- Exporting SRT ---")
    fps = _detect_fps(project, timeline)
    out_name = f"{timeline.GetName()}_subtitles.srt".replace(os.sep, "_")
    out_path = os.path.join(os.path.expanduser("~"), out_name)

    st_index, st_items = _first_nonempty_subtitle_track(timeline)
    if not st_index or not st_items:
        _fail("No subtitle items found after generation; cannot export.")

    if _export_srt_from_subtitle_track(timeline, st_index, st_items, fps, out_path):
        print(f"SRT exported: {out_path}")
    else:
        _fail("Failed to export SRT from subtitle items.")

    print("--- Done ---")


if __name__ == "__main__":
    main()

# -----------------------------------------------------------------------------
# Changelog
# 1.2.0 (2025-09-03): Switched to documented Auto Caption enums; added robust
#                     SRT exporter that iterates subtitle items to avoid relying
#                     on unspecified Export() constants for SRT.
# 1.1.0 (2025-09-03): Added FPS detection and basic audio pre-flight checks.
# 1.0.0 (2025-09-03): Initial version.
# -----------------------------------------------------------------------------
