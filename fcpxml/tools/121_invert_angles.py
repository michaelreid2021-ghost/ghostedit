#!/usr/bin/env python3
# 121_invert_angles.py
# v1.0 (2025-09-03)
# Purpose: Intelligently inverts the two angles of a specified multicam clip in an FCPXML.
# - Swaps the 'name' attributes of the two <mc-angle> elements in the resource definition.
# - Updates the 'angleID' on every <mc-clip> in the timeline spine to reflect the swap.
# - Aborts if the target multicam does not have exactly two angles.

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom
from typing import Dict, Optional

def pretty_write_xml(tree: ET.ElementTree, out_path: Path):
    """Writes the XML tree to a file with standard indentation."""
    rough_string = ET.tostring(tree.getroot(), 'utf-8')
    reparsed = minidom.parseString(rough_string)
    pretty_xml = reparsed.toprettyxml(indent="    ", encoding="utf-8")
    clean_lines = [line for line in pretty_xml.decode('utf-8').split('\n') if line.strip()]
    out_path.write_text('\n'.join(clean_lines), encoding='utf-8')

def invert_multicam_angles(resources: ET.Element, multicam_name: str) -> Optional[Dict[str, str]]:
    """Finds a multicam by name, inverts its two angles, and returns an ID map for the swap."""
    
    media_el = resources.find(f".//media[@name='{multicam_name}']")
    if media_el is None:
        raise RuntimeError(f"Could not find a <media> element named '{multicam_name}'.")
        
    multicam_el = media_el.find('multicam')
    if multicam_el is None:
        raise RuntimeError(f"The media element '{multicam_name}' does not contain a <multicam>.")
        
    angles = multicam_el.findall('mc-angle')
    if len(angles) != 2:
        raise ValueError(f"Expected exactly 2 angles in '{multicam_name}', but found {len(angles)}. Cannot invert.")

    angle1, angle2 = angles[0], angles[1]
    
    name1, id1 = angle1.get('name'), angle1.get('angleID')
    name2, id2 = angle2.get('name'), angle2.get('angleID')

    print(f"-> Found angles to swap: '{name1}' (ID: ...{id1[-6:]}) and '{name2}' (ID: ...{id2[-6:]})")

    # Swap the names in the definition
    angle1.set('name', name2)
    angle2.set('name', name1)
    
    print(f"-> Swapped names in resource definition.")

    # Return a map for updating the spine: {old_id -> new_id}
    return {id1: id2, id2: id1}

def update_spine_angles(spine: ET.Element, multicam_ref_id: str, id_map: Dict[str, str]):
    """Iterates through the timeline and updates the active angleID for all relevant clips."""
    
    update_count = 0
    for mc_clip in spine.findall(f".//mc-clip[@ref='{multicam_ref_id}']"):
        mc_source = mc_clip.find('mc-source')
        if mc_source is None:
            continue
            
        current_id = mc_source.get('angleID')
        if current_id in id_map:
            new_id = id_map[current_id]
            mc_source.set('angleID', new_id)
            update_count += 1
            
    print(f"-> Updated {update_count} clips on the timeline spine.")

def main():
    parser = argparse.ArgumentParser(description="Invert the angles of a 2-angle multicam in an FCPXML file.")
    parser.add_argument('--fcpxml', required=True, help="Path to the input FCPXML file.")
    parser.add_argument('--out', required=True, help="Path for the new, modified FCPXML file.")
    parser.add_argument('--multicam-name', required=True, help="The name of the <media> multicam resource to invert.")
    args = parser.parse_args()

    try:
        tree = ET.parse(args.fcpxml)
        root = tree.getroot()
        resources = root.find('.//resources')
        spine = root.find('.//library/event/project/sequence/spine')

        if resources is None or spine is None:
            raise RuntimeError("Could not find required <resources> or <spine> elements.")
            
        media_to_invert = resources.find(f".//media[@name='{args.multicam_name}']")
        if media_to_invert is None:
             raise RuntimeError(f"Could not find a <media> element named '{args.multicam_name}'.")
        multicam_ref_id = media_to_invert.get('id')

        id_map = invert_multicam_angles(resources, args.multicam_name)
        if id_map:
            update_spine_angles(spine, multicam_ref_id, id_map)

        pretty_write_xml(tree, Path(args.out))
        print(f"\n[+] Success! Inverted FCPXML saved to: {args.out}")

    except Exception as e:
        print(f"\n[!] ERROR: {e}")
        exit(1)

if __name__ == "__main__":
    main()