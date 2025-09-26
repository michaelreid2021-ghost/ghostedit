####
#Dehydrate is used to build a profile of a human editors timing preferences and create a map of his camera angle swaps and time between
#900 Series Files are 'utilities'

import json
import xml.etree.ElementTree as ET
import re
from collections import defaultdict

HUMAN_EDIT_FCPXML = "Info.fcpxml"
OUTPUT_NORMALIZED_FILE = "human_edit_normalized.json"

def fcpxml_duration_to_seconds(duration_str):
    if not duration_str or 's' not in duration_str: return 0.0
    duration_str = duration_str.replace('s', '').strip()
    if '/' in duration_str:
        try: num, den = map(int, duration_str.split('/')); return num / den
        except (ValueError, ZeroDivisionError): return 0.0
    else:
        try: return float(duration_str)
        except ValueError: return 0.0

def main():
    print(f"[+] Normalizing professional edit from '{HUMAN_EDIT_FCPXML}'...")
    tree = ET.parse(HUMAN_EDIT_FCPXML)
    root = tree.getroot()
    
    sequence_start_seconds = fcpxml_duration_to_seconds(root.find('.//sequence').get('tcStart', '0s'))

    ref_to_name_map = {}
    for media in root.findall('.//media'):
        media_id, media_name = media.get('id'), media.get('name', '')
        match = re.search(r'(?:CAM 1 |Take )(\d+)', media_name)
        if match:
            ref_to_name_map[media_id] = f"Take {match.group(1).zfill(2)}"

    a_roll_clips = [c for c in root.findall('.//sequence/spine/mc-clip') if c.get('ref') in ref_to_name_map]
    
    take_clips = defaultdict(list)
    for clip in a_roll_clips:
        take_name = ref_to_name_map[clip.get('ref')]
        take_clips[take_name].append(clip)

    final_output = []
    for take_name, clips in sorted(take_clips.items()):
        if not clips: continue
        
        # The start of the take is the offset of its first clip on the timeline
        take_start_offset = min(fcpxml_duration_to_seconds(c.get('offset')) for c in clips)
        
        events = []
        # Get angle cuts
        for clip in clips:
            offset = fcpxml_duration_to_seconds(clip.get('offset'))
            time_in_take = offset - take_start_offset
            if time_in_take > 0.1: # Exclude the very first clip
                events.append({"type": "angle_cut", "time_in_take": round(time_in_take, 2)})
            
            # Get nested B-roll
            for b_roll_clip in clip.findall('.//asset-clip'):
                clip_name = b_roll_clip.get('name', '').lower()
                if any(kw in clip_name for kw in ['bumper', 'disclaimer', 'card', 'open', '.wav', '.png']):
                    continue
                broll_offset = fcpxml_duration_to_seconds(b_roll_clip.get('offset'))
                broll_duration = fcpxml_duration_to_seconds(b_roll_clip.get('duration'))
                time_in_take_broll = broll_offset - take_start_offset
                if time_in_take_broll >= 0:
                    events.append({"type": "b_roll", "time_in_take": round(time_in_take_broll, 2), "duration": round(broll_duration, 2)})

        unique_events = [dict(t) for t in {tuple(d.items()) for d in events}]
        sorted_events = sorted(unique_events, key=lambda x: x['time_in_take'])
        final_output.append({"take_name": take_name, "events": sorted_events})

    with open(OUTPUT_NORMALIZED_FILE, 'w') as f:
        json.dump(final_output, f, indent=2)

    print(f"[+] Success! Normalized human edit plan saved to '{OUTPUT_NORMALIZED_FILE}'")

if __name__ == "__main__":
    main()