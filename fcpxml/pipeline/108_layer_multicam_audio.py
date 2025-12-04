#!/usr/bin/env python3
# 108_layer_multicam_audio.py
# v2.0 (2025-11-19) - DaVinci Style Split-Source
# Purpose: Locks audio to "Angle 1" by splitting mc-source components within existing clips.
#          Does NOT alter timeline timing, offsets, or durations.
#          Does NOT use lanes. 

import argparse
import copy
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Tuple
from xml.dom import minidom

# ------------------------- Utilities -------------------------

def get_angle_id_by_name(root: ET.Element, ref_id: str, angle_name: str = "Angle 1") -> Optional[str]:
    """Looks up the angleID for a specific angle name inside the media resource definition."""
    media = root.find(f".//resources/media[@id='{ref_id}']")
    if media:
        # Try exact match first
        angle = media.find(f".//mc-angle[@name='{angle_name}']")
        if angle:
            return angle.get('angleID')
        # Fallback: sometimes names have spaces or case diffs, but usually exact is fine for machine generated
    return None

def get_current_video_angle(clip: ET.Element) -> Optional[str]:
    """Finds the angleID of the currently active video source."""
    # Check for existing specific video source
    vid_src = clip.find("./mc-source[@srcEnable='video']")
    if vid_src and vid_src.get('angleID'):
        return vid_src.get('angleID')
    
    # Check for 'all' source
    all_src = clip.find("./mc-source[@srcEnable='all']")
    if all_src and all_src.get('angleID'):
        return all_src.get('angleID')
        
    # Check for ANY source if undefined
    any_src = clip.find("./mc-source")
    if any_src and any_src.get('angleID'):
        return any_src.get('angleID')
        
    return None

def create_mc_source(angle_id: str, enable_mode: str, template_source: Optional[ET.Element]) -> ET.Element:
    """Creates a new mc-source element, copying transforms from template if available."""
    new_src = ET.Element('mc-source')
    new_src.set('angleID', angle_id)
    new_src.set('srcEnable', enable_mode)
    
    if template_source is not None:
        # Copy adjust-transform and adjust-conform if they exist
        for child in template_source:
            if child.tag in ['adjust-transform', 'adjust-conform']:
                new_src.append(copy.deepcopy(child))
    else:
        # Default transform if we have absolutely nothing to clone (rare)
        tf = ET.SubElement(new_src, 'adjust-transform')
        tf.set('scale', '1 1')
        tf.set('anchor', '0 0')
        tf.set('position', '0 0')
        
    return new_src

def validate_and_prettify(tree: ET.ElementTree) -> str:
    rough = ET.tostring(tree.getroot(), encoding='unicode')
    reparsed = ET.fromstring(rough)
    return minidom.parseString(ET.tostring(reparsed)).toprettyxml(indent="  ")

# ------------------------- Core Logic -------------------------

def process_timeline(tree: ET.ElementTree) -> Tuple[bool, str]:
    root = tree.getroot()
    spine = root.find('.//spine')
    if spine is None:
        return False, ""

    changed_count = 0

    # Iterate over every mc-clip in the spine
    for clip in spine.findall('mc-clip'):
        ref_id = clip.get('ref')
        if not ref_id:
            continue

        # 1. Identify "Angle 1" (The Audio Bed Target)
        angle_1_id = get_angle_id_by_name(root, ref_id, "Angle 1")
        if not angle_1_id:
            print(f"[WARN] Could not find 'Angle 1' for clip {clip.get('name')}. Skipping.")
            continue

        # 2. Identify Current Video Angle (The Visual Target)
        current_video_id = get_current_video_angle(clip)
        if not current_video_id:
            # If no angle is set, it likely defaults to Angle 1, but let's be explicit
            current_video_id = angle_1_id

        # 3. Capture existing transforms/settings to clone
        # We grab the first available source to use as a template for transforms
        template_src = clip.find('mc-source')

        # 4. WIPE existing sources
        # We are rewriting the wiring completely to guarantee structure
        for child in list(clip):
            if child.tag == 'mc-source':
                clip.remove(child)

        # 5. INJECT New Split Sources
        
        # Source A: VIDEO (Current Angle)
        # We insert Video first (convention, though order technically doesn't matter to XML parser)
        vid_source = create_mc_source(current_video_id, 'video', template_src)
        clip.append(vid_source)

        # Source B: AUDIO (Angle 1)
        aud_source = create_mc_source(angle_1_id, 'audio', template_src)
        clip.append(aud_source)

        changed_count += 1

    if changed_count > 0:
        print(f"[+] Updated {changed_count} clips to Split-Source (Audio Locked to Angle 1).")
        return True, validate_and_prettify(tree)
    
    return False, ""

# ------------------------- CLI -------------------------

def main():
    parser = argparse.ArgumentParser(description="Lock Multicam Audio to Angle 1 using Split-Source (DaVinci Style).")
    parser.add_argument("--fcpxml", required=True, help="Input FCPXML.")
    parser.add_argument("--out", required=True, help="Output FCPXML.")
    
    args = parser.parse_args()

    in_path = Path(args.fcpxml).resolve()
    out_path = Path(args.out).resolve()

    if not in_path.exists():
        print(f"[!] Input not found: {in_path}")
        sys.exit(1)

    tree = ET.parse(str(in_path))
    changed, xml_str = process_timeline(tree)

    if changed:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(xml_str)
        print(f"[SUCCESS] Wrote to {out_path}")
    else:
        print("[i] No multicam clips found or no changes needed.")
        # Copy original if no changes
        if in_path != out_path:
             with open(out_path, 'w', encoding='utf-8') as f:
                f.write(ET.tostring(tree.getroot(), encoding='unicode'))

if __name__ == "__main__":
    main()