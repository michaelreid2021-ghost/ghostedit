#!/usr/bin/env python3
# 01_dehydrate.py
# Final, proxy-driven version.
# This script scans for existing proxy files and matches them to their
# corresponding 'mc-clip' usage on the timeline to build the manifest.

import xml.etree.ElementTree as ET
import json
import argparse
from pathlib import Path
from project_paths import add_base_arg, from_args

def fcpxml_time_to_seconds(time_str):
    """
    Converts FCPXML time format 'numerator/denominators' (e.g., '5442437/24000s') to seconds.
    """
    if not time_str or 's' not in time_str:
        return 0.0
    time_str = time_str.replace('s', '').strip()
    if '/' in time_str:
        try:
            num, den = map(int, time_str.split('/'))
            # Handle potential rational number format like '147147/1000'
            if den == 0: return 0.0
            return num / den
        except (ValueError, ZeroDivisionError):
            return 0.0
    else:
        try:
            return float(time_str)
        except ValueError:
            return 0.0

def main():
    parser = argparse.ArgumentParser(description="Dehydrate an FCPXML by matching existing proxy files to timeline mc-clips.")
    add_base_arg(parser)
    parser.add_argument("--fcpxml", type=Path, default=None, help="Path to Info.fcpxml")
    parser.add_argument("--out", type=Path, default=None, help="Path to master_manifest.json")
    parser.add_argument("--phase", type=Path, default="phase_1", help="Path to phase")
    args = parser.parse_args()

    p = from_args(args)
    template_fcpxml = args.fcpxml or p.info_fcpxml
    master_manifest_path = args.out or p.master_manifest
    proxies_dir = p.proxies

    print(f"[+] Starting proxy-driven dehydration of template: {template_fcpxml}")
    print(f"  base: {p.base}")
    print(f"  proxies dir: {proxies_dir}")

    if not template_fcpxml.exists():
        print(f"[!] FATAL ERROR: Template FCPXML not found at '{template_fcpxml}'")
        return

    try:
        tree = ET.parse(str(template_fcpxml))
        root = tree.getroot()
    except ET.ParseError as e:
        print(f"[!] FATAL ERROR: Could not parse XML file. Error: {e}")
        return

    # --- Step 1: Scan for existing proxy files ---
    if not proxies_dir.exists():
        print(f"[!] FATAL ERROR: Proxies directory not found at '{proxies_dir}'")
        return
    existing_proxies = [f for f in proxies_dir.iterdir() if f.is_file() and f.suffix.lower() in ('.mov', '.mp4')]
    print(f"[i] Found {len(existing_proxies)} potential proxy files in '{proxies_dir}'.")

    # --- Step 2: Build lookup maps from FCPXML ---
    assets_by_id = {
        asset.get('id'): asset
        for asset in root.findall('.//resources/asset')
        if asset.get('name', '').lower().endswith(('.braw', '.mov'))
    }
    asset_by_basename = {Path(asset.get('name')).stem: asset_id for asset_id, asset in assets_by_id.items()}
    
    multicam_defs = {
        media.get('id'): {
            "name": media.get('name'),
            "source_asset_ids": [
                aclip.get('ref') for aclip in media.findall('.//multicam/mc-angle/asset-clip')
                if aclip.get('ref') in assets_by_id
            ]
        }
        for media in root.findall('.//resources/media[multicam]')
    }

    asset_to_multicam_map = {
        asset_id: mc_id
        for mc_id, mc_data in multicam_defs.items()
        for asset_id in mc_data["source_asset_ids"]
    }

    timeline_mc_clips = root.findall('.//sequence/spine//mc-clip')

    # --- Step 3: Match proxies to timeline clips ---
    scenes_to_process = []
    processed_clip_uids = set()

    for proxy_file in existing_proxies:
        proxy_basename = proxy_file.stem
        source_asset_id = asset_by_basename.get(proxy_basename)
        if not source_asset_id:
            continue # This proxy has no matching source asset in the project

        parent_multicam_id = asset_to_multicam_map.get(source_asset_id)
        if not parent_multicam_id:
            continue # This proxy's source asset is not part of any multicam definition

        # Find every instance of this multicam clip on the timeline
        for mc_clip in timeline_mc_clips:
            if mc_clip.get('ref') == parent_multicam_id:
                offset_str = mc_clip.get('offset', '0/s')
                offset_val = fcpxml_time_to_seconds(offset_str)
                # Unique ID for a clip is its position on the timeline (offset and track/lane)
                clip_uid = f"{mc_clip.get('lane', '1')}:{offset_str}"

                if clip_uid not in processed_clip_uids:
                    processed_clip_uids.add(clip_uid)
                    scenes_to_process.append({
                        "mc_clip_element": mc_clip,
                        "multicam_id": parent_multicam_id,
                        "proxy_filename": proxy_file.name,
                        "offset": offset_val
                    })
    
    # --- Step 4: Sort scenes by timeline order and build the final manifest ---
    scenes_to_process.sort(key=lambda x: x['offset'])
    
    master_manifest = []
    for i, scene in enumerate(scenes_to_process, 1):
        mc_clip = scene["mc_clip_element"]
        multicam_def = multicam_defs[scene["multicam_id"]]
        
        source_files = []
        for asset_id in multicam_def["source_asset_ids"]:
            asset = assets_by_id[asset_id]
            source_files.append({
                "source_filename": asset.get('name'),
                "asset_id": asset_id
            })

        scene_data = {
            "scene_number": i,
            "is_multicam": True,
            "multicam_name": multicam_def["name"],
            "proxy_filename": scene["proxy_filename"],
            "source_files": source_files,
            "duration_seconds": fcpxml_time_to_seconds(mc_clip.get('duration', '0/s'))
        }
        master_manifest.append(scene_data)

    if not master_manifest:
        print("[!] Dehydration resulted in an empty manifest. No proxies could be matched to timeline clips.")
        return

    master_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    master_manifest_path.write_text(json.dumps(master_manifest, indent=4), encoding="utf-8")

    print(f"\n[+] Success! Dehydrated manifest created at '{master_manifest_path}'")
    print(f"[i] Total scenes cataloged: {len(master_manifest)}")

if __name__ == "__main__":
    main()