#!/usr/bin/env python3
# 117_timeline_redactor.py
# v1.1 (2025-09-08)
# - Removes a specified multicam clip and its unused resources from the timeline.
# - Correctly recalculates the offsets of all subsequent clips to close the gap.
# - Updates the final sequence duration.

import argparse
import xml.etree.ElementTree as ET
import shutil
from pathlib import Path
from fractions import Fraction
from xml.dom import minidom
from typing import Tuple, List, Dict, Any

# --- Helper Classes & Functions (for consistency with pipeline) ---

def parse_fcpx_frac(s: str) -> Fraction:
    """Parses an FCPXML time string (e.g., '1001/24000s') into a Fraction."""
    s = s.strip()
    if not s.endswith('s'):
        raise ValueError(f"Invalid FCPXML time format: {s}")
    body = s[:-1]
    if '/' in body:
        n, d = body.split('/')
        return Fraction(int(n), int(d))
    return Fraction(body)

def frames_to_fcpx_frac(frames: int, tb_num: int, tb_den: int) -> str:
    """Converts an integer frame count to an FCPXML time string."""
    f = Fraction(frames * tb_den, tb_num)
    return f"{f.numerator}/{f.denominator}s"

def frac_to_frames(frac_sec: Fraction, tb_num: int, tb_den: int) -> int:
    """Converts a time Fraction into an integer frame count."""
    x = frac_sec * Fraction(tb_num, tb_den)
    return int(round(x.numerator / x.denominator))

def pretty_write_xml(tree: ET.ElementTree, out_path: Path):
    """Writes the XML tree to a file with clean formatting."""
    rough_string = ET.tostring(tree.getroot(), 'utf-8')
    reparsed = minidom.parseString(rough_string)
    pretty_xml = reparsed.toprettyxml(indent="    ", newl="\n", encoding="utf-8")
    text_lines = [line for line in pretty_xml.decode('utf-8').split('\n') if line.strip()]
    out_path.write_text('\n'.join(text_lines), encoding="utf-8")

def timebase_from_sequence(resources_root: ET.Element, seq_elem: ET.Element) -> Tuple[int, int]:
    """Extracts the timebase (numerator, denominator) from the sequence's format."""
    fmt_id = seq_elem.get('format')
    if not fmt_id: raise RuntimeError("Sequence missing 'format' attribute.")
    fmt = resources_root.find(f".//format[@id='{fmt_id}']")
    if fmt is None: raise RuntimeError(f"Could not find <format id='{fmt_id}'> in resources.")
    fd = fmt.get('frameDuration')
    if not fd: raise RuntimeError("Format missing 'frameDuration'.")
    frac = parse_fcpx_frac(fd)
    return (frac.denominator, frac.numerator)

def recalculate_spine_and_duration(sequence: ET.Element, tb_num: int, tb_den: int):
    """
    Iterates through the spine, recalculates all clip offsets to ensure they
    are contiguous, and then updates the final sequence duration.
    """
    spine = sequence.find('./spine')
    if spine is None or len(spine) == 0:
        sequence.set('duration', '0/1s')
        print("Spine is empty. Sequence duration set to 0.")
        return

    clips = [el for el in spine if el.tag.endswith('clip') and el.get('duration')]
    if not clips:
        sequence.set('duration', '0/1s')
        print("Spine has no clips with duration. Sequence duration set to 0.")
        return

    # Start with the offset of the very first clip
    current_offset_f = parse_fcpx_frac(clips[0].get('offset', '0/1s'))
    
    for clip_el in clips:
        clip_el.set('offset', f"{current_offset_f.numerator}/{current_offset_f.denominator}s")
        duration_f = parse_fcpx_frac(clip_el.get('duration', '0/1s'))
        current_offset_f += duration_f

    # The new total duration is the end point of the last clip
    total_duration_frames = frac_to_frames(current_offset_f, tb_num, tb_den)
    
    # Adjust for tcStart if necessary
    tc_start_frames = frac_to_frames(parse_fcpx_frac(sequence.get('tcStart', '0/1s')), tb_num, tb_den)
    final_duration_frames = max(0, total_duration_frames - tc_start_frames)

    sequence.set('duration', frames_to_fcpx_frac(final_duration_frames, tb_num, tb_den))
    print(f"Spine recalculated. New sequence duration is {final_duration_frames} frames.")


# --- Main Redaction Logic ---
def main():
    parser = argparse.ArgumentParser(description="Removes a media clip and its unused assets from an FCPXML file.")
    parser.add_argument('--fcpxml', required=True, type=Path, help="Path to the input FCPXML file.")
    parser.add_argument('--out', required=True, type=Path, help="Path to write the modified FCPXML file.")
    parser.add_argument('--media-name', required=True, help="The 'name' of the multicam media to redact.")
    args = parser.parse_args()

    tree = ET.parse(args.fcpxml)
    root = tree.getroot()
    resources = root.find('.//resources')
    sequence = root.find('.//sequence')
    spine = sequence.find('./spine')

    if any(el is None for el in [resources, sequence, spine]):
        raise RuntimeError("FCPXML is missing critical elements (resources, sequence, or spine).")
        
    tb_num, tb_den = timebase_from_sequence(resources, sequence)

    # 1. Find the <media> resource by name to get its ID
    media_to_remove = resources.find(f".//media[@name='{args.media_name}']")
    if media_to_remove is None:
        print(f"Warning: Media named '{args.media_name}' not found. No changes made.")
        shutil.copy(args.fcpxml, args.out)
        return
        
    media_id = media_to_remove.get('id')
    print(f"Found media '{args.media_name}' with ID '{media_id}'. Preparing for redaction.")

    # 2. Find and remove the corresponding clip from the timeline spine
    clip_on_spine = spine.find(f"./mc-clip[@ref='{media_id}']")
    if clip_on_spine is not None:
        spine.remove(clip_on_spine)
        print(f" -> Removed clip with ref='{media_id}' from the timeline spine.")
    else:
        print(f" -> No clip with ref='{media_id}' found on the spine. Only removing resources.")

    # 3. Collect all asset IDs used by the media before removing it
    assets_to_check = {
        clip.get('ref') for clip in media_to_remove.findall('.//asset-clip') if clip.get('ref')
    }
    print(f" -> Media references these asset IDs: {assets_to_check}")

    # 4. Remove the <media> element itself
    resources.remove(media_to_remove)
    print(f" -> Removed <media> element with ID '{media_id}'.")

    # 5. Garbage collect any assets that are now unused
    for asset_id in assets_to_check:
        # Check if any *other* clip in the entire project still uses this asset
        if root.find(f".//asset-clip[@ref='{asset_id}']") is None:
            asset_to_remove = resources.find(f".//asset[@id='{asset_id}']")
            if asset_to_remove is not None:
                asset_name = asset_to_remove.get('name', 'Unnamed')
                resources.remove(asset_to_remove)
                print(f" -> Asset '{asset_id}' ('{asset_name}') is no longer in use. Removing.")
        else:
            print(f" -> Asset '{asset_id}' is still in use by another clip. Keeping.")
            
    # 6. Recalculate all timeline offsets and the final sequence duration
    recalculate_spine_and_duration(sequence, tb_num, tb_den)
    
    # 7. Save the modified file
    pretty_write_xml(tree, args.out)
    print(f"\nSuccessfully redacted '{args.media_name}' and wrote to: {args.out}")

if __name__ == '__main__':
    main()
