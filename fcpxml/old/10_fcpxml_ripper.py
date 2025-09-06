import os
import re
from xml.etree import ElementTree as ET
import argparse
from pathlib import Path
from project_paths import add_base_arg, from_args

# --- HELPER FUNCTION (for consistency with other scripts) ---
def _normalize_title(s: str) -> str:
    """Normalizes a clip name for comparison against the ignore list."""
    if not s:
        return ""
    s = os.path.splitext(s)[0]
    s = re.sub(r'^\s*\d+[_\-\s]+', '', s)  # drop leading "NN_" or "NN-"
    s = s.replace('_', ' ')
    s = re.sub(r'\s+', ' ', s).strip()
    return s.lower()

def read_ignore_list(ignore_file: Path) -> set[str]:
    """Reads the .ai_ignore file and returns a SET of NORMALIZED clip names."""
    if not ignore_file.exists():
        return set()
    
    ignore_set = set()
    with open(ignore_file, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        for line in lines:
            name = line.split(',')[0].strip()
            if name:
                ignore_set.add(_normalize_title(name))

    print(f"[i] Loaded {len(ignore_set)} normalized names to ignore/preserve from '{ignore_file}'.")
    return ignore_set

def main():
    """
    Parses an FCPXML file, splitting it into structural parts and separating
    timeline clips into 'ignored_clips' and 'process_clips' folders.
    """
    parser = argparse.ArgumentParser(description="Split FCPXML into structural parts and per-clip fragments.")
    add_base_arg(parser)
    parser.add_argument("--fcpxml", type=Path, default=None, help="Path to Info.fcpxml")
    args = parser.parse_args()

    p = from_args(args)
    fcpx_path = args.fcpxml or p.info_fcpxml
    out_dir = p.dehydrated_tpl
    ignored_dir = p.ignored_clips
    process_dir = p.process_clips
    ignore_file = p.ai_ignore

    print(f"--- Starting FCPXML Ripper (Step 10) ---")
    print(f"  base: {p.base}")
    print(f"  fcpxml: {fcpx_path}")
    print(f"  out_dir: {out_dir}")
    print(f"  ignore_file: {ignore_file}")
    
    if not fcpx_path.exists():
        print(f"[!] FATAL ERROR: Template file '{fcpx_path}' not found.")
        return

    os.makedirs(ignored_dir, exist_ok=True)
    os.makedirs(process_dir, exist_ok=True)
    print(f"[+] Ensured output directories exist.")

    ignore_set = read_ignore_list(ignore_file)
    
    ET.register_namespace('', '')
    tree = ET.parse(str(fcpx_path))
    root = tree.getroot()

    # --- 1. Extract and Write Static Parts ---
    # (This part of the code is correct and remains unchanged)
    with open(fcpx_path, 'r', encoding='utf-8') as f:
        full_content = f.read()
        header_content = full_content.split('<resources>')[0].strip()
    header_path = out_dir / "00_header.xml"
    header_path.write_text(header_content, encoding='utf-8')
    print(f"    -> Wrote header to '{header_path}'")

    resources_element = root.find('resources')
    if resources_element is not None:
        resources_path = out_dir / "01_resources.xml"
        resources_path.write_text(ET.tostring(resources_element, encoding='unicode'), encoding='utf-8')
        print(f"    -> Wrote resources block to '{resources_path}'")
        
    library_open_content = f"""<library><event name="{root.find('.//event').get('name')}"><project name="{root.find('.//project').get('name')}"><sequence tcFormat="{root.find('.//sequence').get('tcFormat')}" duration="placeholder" format="{root.find('.//sequence').get('format')}" tcStart="{root.find('.//sequence').get('tcStart')}"><spine>"""
    library_open_path = out_dir / "02_library_open.xml"
    library_open_path.write_text(library_open_content, encoding='utf-8')
    print(f"    -> Wrote library/spine opening tags to '{library_open_path}'")

    footer_content = """</spine></sequence></project></event></library></fcpxml>"""
    footer_path = out_dir / "99_footer.xml"
    footer_path.write_text(footer_content, encoding='utf-8')
    print(f"    -> Wrote footer to '{footer_path}'")

    # --- 2. Process the Spine to separate clips into individual files ---
    spine_element = root.find('.//spine')
    if spine_element is not None:
        print("    -> Processing <spine> contents...")
        for i, clip_element in enumerate(spine_element):
            clip_name = clip_element.get('name')
            clip_xml_string = ET.tostring(clip_element, encoding='unicode').strip()
            
            safe_name = re.sub(r'[^\w\.-]', '_', clip_name) if clip_name else f"element_{i}"
            filename = f"{str(i).zfill(2)}_{safe_name}.xml"

            # --- CORRECTED NORMALIZED LOGIC ---
            normalized_name = _normalize_title(clip_name)
            # An element is ignored if its normalized name is in the ignore set OR if it's an asset-clip.
            if (normalized_name in ignore_set) or (clip_element.tag == 'asset-clip'):
                filepath = ignored_dir / filename
                print(f"        -> Storing ignored clip '{clip_name}' to '{filepath}'")
            else:
                filepath = process_dir / filename
                print(f"        -> Storing process clip '{clip_name}' to '{filepath}'")
            
            filepath.write_text(clip_xml_string, encoding='utf-8')

    print(f"\n[+] Success! Dehydration Step 10 complete. Parts are in '{out_dir}'.")

if __name__ == "__main__":
    main()