import os
import re
from xml.etree import ElementTree as ET
import json
import argparse
from pathlib import Path
from project_paths import add_base_arg, from_args

# --- Configuration ---
FCPXML_TEMPLATE_FILE = "Info.fcpxml"  # legacy default
ENRICHED_MANIFEST_FILE = "15_enriched_manifest.json"  # updated default name

# --- HELPER FUNCTIONS (from 47_build_angle_changes.py) ---
def _normalize_title(s: str) -> str:
    """Normalizes a clip name for comparison against the ignore list."""
    s = os.path.splitext(s)[0]
    s = re.sub(r'^\s*\d+[_\-\s]+', '', s)  # drop leading "NN_" or "NN-"
    s = s.replace('_', ' ')
    s = re.sub(r'\s+', ' ', s).strip()
    return s.lower()

def load_ignore_names(project_root: Path) -> set[str]:
    """Reads .ai_ignore from project root and working directory to build an ignore set."""
    ignore_set: set[str] = set()
    candidates = [
        project_root / ".ai_ignore",
        project_root / "ai_working_files" / ".ai_ignore"
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    name = line.split(",", 1)[0].strip()
                    if name:
                        ignore_set.add(_normalize_title(name))
        except Exception as e:
            print(f"[!] Warning: Could not read or parse ignore file at '{path}': {e}")
            pass
    return ignore_set

def main():
    parser = argparse.ArgumentParser(description="Create enriched manifest mapping take numbers to media and angle IDs.")
    add_base_arg(parser)
    parser.add_argument("--fcpxml", type=Path, default=None, help="Path to template Info.fcpxml")
    parser.add_argument("--out", type=Path, default=None, help="Path to write 15_enriched_manifest.json")
    args = parser.parse_args()

    p = from_args(args)
    fcpx_path = args.fcpxml or p.info_fcpxml
    out_path = args.out or p.enriched_manifest

    print(f"--- Starting Manifest Enrichment (Step 15) ---")
    print(f"  base: {p.base}")
    print(f"  fcpxml: {fcpx_path}")
    print(f"  out: {out_path}")
    
    if not fcpx_path.exists():
        print(f"[!] FATAL ERROR: Template file '{fcpx_path}' not found.")
        return

    # --- ADDED: Load ignore list ---
    project_root = Path(p.base)
    ignore_names = load_ignore_names(project_root)
    if ignore_names:
        print(f"[i] Loaded {len(ignore_names)} names from .ai_ignore")

    ET.register_namespace('', '')
    tree = ET.parse(str(fcpx_path))
    root = tree.getroot()

    enriched_manifest = {}
    
    resources = root.find('resources')
    if resources is None:
        print("[!] FATAL ERROR: Could not find <resources> block in template.")
        return

    for media in resources.findall('media'):
        multicam = media.find('multicam')
        if multicam is None:
            continue

        media_name = media.get('name')
        
        # --- ADDED: Check against ignore list ---
        normalized_name = _normalize_title(media_name)
        if normalized_name in ignore_names:
            print(f"    -> Ignoring '{media_name}' (matched '{normalized_name}')")
            continue

        media_ref_id = media.get('id')
        
        match = re.search(r'\b(\d{2})\b', media_name)
        if not match:
            continue
        take_number = match.group(1)

        take_data = {
            "media_ref_id": media_ref_id,
            "angle_A_id": None, # Corresponds to CAM 1
            "angle_B_id": None  # Corresponds to CAM 2
        }

        for angle in multicam.findall('mc-angle'):
            asset_clip = angle.find('.//asset-clip')
            if asset_clip is not None:
                asset_name = asset_clip.get('name', '')
                angle_id = angle.get('angleID')
                if "CAM 1" in asset_name:
                    take_data["angle_A_id"] = angle_id
                elif "CAM 2" in asset_name:
                    take_data["angle_B_id"] = angle_id
        
        if take_data["angle_A_id"] and take_data["angle_B_id"]:
            enriched_manifest[take_number] = take_data
            print(f"    -> Processed Take {take_number}: Found CAM 1 and CAM 2 angle IDs.")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps( enriched_manifest, indent=2), encoding='utf-8')

    print(f"\n[+] Success! Enriched manifest created at '{out_path}'")

if __name__ == "__main__":
    main()