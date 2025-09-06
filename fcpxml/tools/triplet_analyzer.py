#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: 04b_role_triplet_stage1.py
# Version: 1.0.0
# Last Modified: 2025-08-23
# Purpose: Stage-1 labeling pass over an already enriched scene manifest (list of scenes with
#          timed_transcript + audio_features). Learns a HOST audio profile per scene, assigns
#          predicted roles (HOST/DIRECTOR/STAFF/UNKNOWN), finds short D↔H/S back-and-forth
#          "triplets", groups non-HOST chatter blocks, and writes results back into the same
#          manifest file—after backing it up to *_stage1.json to preserve pipeline traces.
#
# Usage:
#   python 04b_role_triplet_stage1.py \
#       --manifest scene_transcribed_enriched.json
#
# Behavior:
#   - Loads <manifest>, which must be a JSON list of scene objects. Each scene should include
#     a "timed_transcript": [ { "id", "start", "end", "text", "audio_features": {...} }, ... ].
#   - For each scene:
#       * learns a HOST centroid from audio-only seeds (no production notes needed)
#       * assigns predicted_role_stage1 + role_confidence_stage1 per segment
#       * emits scene["role_triplet_stage1"] with summary/groups/triplets/params
#   - Writes a backup file next to the manifest with suffix "_stage1.json"
#     (e.g., scene_transcribed_enriched_stage1.json), then overwrites the manifest in place.
#
# Notes:
#   - This step is deliberately "pre-production-notes": it does NOT require clean_takes.
#   - It does not rely on any external dependencies beyond the standard library.

from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------
# Data Models
# ---------------------------------

@dataclass
class AudioFeatures:
    energy_mean: float = 0.0
    energy_p95: float = 0.0
    zcr_mean: float = 0.0
    centroid_mean_hz: float = 0.0
    flatness_mean: float = 0.0
    directness_score: float = 0.0
    pitch_mean_hz: Optional[float] = None
    voiced_fraction: float = 0.0
    silence_before_s: float = 0.0
    silence_after_s: float = 0.0
    energy_peak_count: float = 0.0
    energy_band: Optional[str] = None
    role_hint: Optional[str] = None

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AudioFeatures":
        return AudioFeatures(
            energy_mean=float(d.get("energy_mean", 0.0) or 0.0),
            energy_p95=float(d.get("energy_p95", 0.0) or 0.0),
            zcr_mean=float(d.get("zcr_mean", 0.0) or 0.0),
            centroid_mean_hz=float(d.get("centroid_mean_hz", 0.0) or 0.0),
            flatness_mean=float(d.get("flatness_mean", 0.0) or 0.0),
            directness_score=float(d.get("directness_score", 0.0) or 0.0),
            pitch_mean_hz=(float(d["pitch_mean_hz"]) if d.get("pitch_mean_hz") is not None else None),
            voiced_fraction=float(d.get("voiced_fraction", 0.0) or 0.0),
            silence_before_s=float(d.get("silence_before_s", 0.0) or 0.0),
            silence_after_s=float(d.get("silence_after_s", 0.0) or 0.0),
            energy_peak_count=float(d.get("energy_peak_count", 0.0) or 0.0),
            energy_band=d.get("energy_band"),
            role_hint=d.get("role_hint"),
        )


@dataclass
class Segment:
    id: Any
    start: float
    end: float
    text: str
    audio: AudioFeatures
    role_hint: Optional[str] = None

    @property
    def duration(self) -> float:
        return max(0.0, float(self.end) - float(self.start))


# ---------------------------------
# Utilities
# ---------------------------------

def clamp(a: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, a))


def safe_mean(values: List[float], default=0.0) -> float:
    arr = [v for v in values if v is not None and not math.isnan(v)]
    return statistics.fmean(arr) if arr else default


def stddev(values: List[float], default=1.0) -> float:
    arr = [v for v in values if v is not None and not math.isnan(v)]
    return statistics.pstdev(arr) if arr else default


def euclidean(a: List[float], b: List[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def centroid(vectors: List[List[float]]) -> List[float]:
    if not vectors:
        return [0.0] * 11
    n = len(vectors)
    m = len(vectors[0])
    return [sum(v[i] for v in vectors) / n for i in range(m)]


# ---------------------------------
# Feature Engineering
# ---------------------------------

_FEATURE_KEYS = [
    "energy_mean",
    "energy_p95",
    "zcr_mean",
    "centroid_mean_hz",
    "flatness_mean",
    "directness_score",
    "pitch_mean_hz_filled",
    "voiced_fraction",
    "silence_before_s",
    "silence_after_s",
    "energy_peak_rate",
]

def featurize(seg: Segment) -> Dict[str, float]:
    dur = max(1e-6, seg.duration)
    pitch_filled = seg.audio.pitch_mean_hz if seg.audio.pitch_mean_hz is not None else 0.0
    return {
        "energy_mean": seg.audio.energy_mean,
        "energy_p95": seg.audio.energy_p95,
        "zcr_mean": seg.audio.zcr_mean,
        "centroid_mean_hz": seg.audio.centroid_mean_hz,
        "flatness_mean": seg.audio.flatness_mean,
        "directness_score": seg.audio.directness_score,
        "pitch_mean_hz_filled": pitch_filled,
        "voiced_fraction": seg.audio.voiced_fraction,
        "silence_before_s": seg.audio.silence_before_s,
        "silence_after_s": seg.audio.silence_after_s,
        "energy_peak_rate": float(seg.audio.energy_peak_count) / dur if dur > 0 else 0.0,
    }

def standardize(rows: List[Dict[str, float]]) -> Tuple[List[List[float]], Dict[str, Tuple[float, float]]]:
    stats: Dict[str, Tuple[float, float]] = {}
    for k in _FEATURE_KEYS:
        col = [r.get(k, 0.0) for r in rows]
        mu = safe_mean(col, 0.0)
        sd = stddev(col, 1.0)
        stats[k] = (mu, sd if sd > 1e-6 else 1.0)
    matrix: List[List[float]] = []
    for r in rows:
        vec = []
        for k in _FEATURE_KEYS:
            mu, sd = stats[k]
            vec.append((r.get(k, 0.0) - mu) / sd)
        matrix.append(vec)
    return matrix, stats


# ---------------------------------
# Host Profile
# ---------------------------------

def is_seed_host(seg: Segment) -> bool:
    # Seed from hints or sustained confident voice
    hint = (seg.role_hint or seg.audio.role_hint or "") or ""
    if hint.lower() == "host":
        return True
    af = seg.audio
    return (seg.duration >= 5.0 and af.directness_score >= 0.82 and af.voiced_fraction >= 0.55)

def build_host_profile(segments: List[Segment]) -> Tuple[List[float], List[int], float]:
    feats: List[Dict[str, float]] = []
    seed_idx: List[int] = []
    for i, s in enumerate(segments):
        if is_seed_host(s):
            seed_idx.append(i)
            feats.append(featurize(s))
    if not feats:
        # fallback: top-5 longest voiced segments
        candidates = sorted(
            [(i, s) for i, s in enumerate(segments)],
            key=lambda t: (t[1].duration * t[1].audio.voiced_fraction, t[1].audio.directness_score),
            reverse=True,
        )[:5]
        seed_idx = [i for i, _ in candidates]
        feats = [featurize(s) for _, s in candidates]
    mat, _ = standardize(feats)
    center = centroid(mat)
    dists = [euclidean(v, center) for v in mat] or [0.0]
    thresh = (safe_mean(dists, 0.0) + 1.25 * stddev(dists, 0.0))
    return center, seed_idx, float(thresh)


# ---------------------------------
# Minimal K-Means
# ---------------------------------

def kmeans(vectors: List[List[float]], k: int, iters: int = 25) -> List[int]:
    if not vectors:
        return []
    n = len(vectors)
    centers = [vectors[i * (n // k)][:] if (i * (n // k)) < n else vectors[i % n][:] for i in range(k)]
    labels = [0] * n
    for _ in range(iters):
        changed = False
        for i, v in enumerate(vectors):
            best, bestd = 0, float("inf")
            for ci, c in enumerate(centers):
                d = euclidean(v, c)
                if d < bestd:
                    best, bestd = ci, d
            if labels[i] != best:
                labels[i] = best
                changed = True
        clusters = [[] for _ in range(k)]
        for i, lab in enumerate(labels):
            clusters[lab].append(vectors[i])
        for ci in range(k):
            if clusters[ci]:
                m = len(clusters[ci][0])
                centers[ci] = [sum(vec[j] for vec in clusters[ci]) / len(clusters[ci]) for j in range(m)]
            else:
                centers[ci] = vectors[ci % n][:]
        if not changed:
            break
    return labels


# ---------------------------------
# Role Assignment
# ---------------------------------

def assign_roles(segments: List[Segment], host_center: List[float], host_thresh: float) -> Tuple[List[str], List[float]]:
    rows = [featurize(s) for s in segments]
    mat, _ = standardize(rows)
    host_dists = [euclidean(v, host_center) for v in mat]

    roles = ["UNDECIDED"] * len(segments)
    for i, s in enumerate(segments):
        hint = (s.role_hint or s.audio.role_hint or "") or ""
        if hint.lower() == "host":
            roles[i] = "HOST"
            continue
        if host_dists[i] <= host_thresh:
            roles[i] = "HOST"

    # Cluster the rest into DIRECTOR vs STAFF; UNKNOWN for low-voice/noise
    non_idx = [i for i, r in enumerate(roles) if r != "HOST"]
    if non_idx:
        non_vecs = [mat[i] for i in non_idx]
        labels = kmeans(non_vecs, k=2, iters=30)
        # Decide which cluster is DIRECTOR by "director-ish" cues
        counts = {0: 0.0, 1: 0.0}
        for j, idx in enumerate(non_idx):
            s = segments[idx]
            hint = (s.role_hint or s.audio.role_hint or "") or ""
            if hint.startswith("director"):
                counts[labels[j]] += 1.0
            if (s.audio.energy_band in ("low", "medium")) and (s.audio.directness_score >= 0.82):
                counts[labels[j]] += 0.25
        director_label = 0 if counts[0] >= counts[1] else 1

        for j, idx in enumerate(non_idx):
            s = segments[idx]
            is_noise = (s.audio.voiced_fraction <= 0.2) or ((not (s.text or "").strip()) and s.audio.flatness_mean >= 0.03)
            if is_noise:
                roles[idx] = "UNKNOWN"
            else:
                roles[idx] = "DIRECTOR" if labels[j] == director_label else "STAFF"

    # Confidence
    confidences: List[float] = []
    for i, r in enumerate(roles):
        if r == "HOST":
            d = host_dists[i]
            conf = clamp(1.0 - (d / (host_thresh + 1e-6)), 0.0, 1.0)
        elif r in ("DIRECTOR", "STAFF"):
            s = segments[i]
            base = 0.5 if r == "STAFF" else 0.6
            conf = clamp(base + 0.25 * (s.audio.directness_score - 0.8) + 0.15 * (s.audio.voiced_fraction - 0.5), 0.0, 1.0)
        else:
            conf = 0.3
        confidences.append(round(float(conf), 4))

    return roles, confidences


# ---------------------------------
# Grouping & Triplets
# ---------------------------------

def group_non_host_blocks(segments: List[Segment], roles: List[str], max_gap: float = 1.0) -> List[Dict[str, Any]]:
    items = [(i, seg) for i, seg in enumerate(segments) if roles[i] != "HOST"]
    groups: List[Dict[str, Any]] = []
    if not items:
        return groups
    acc: List[int] = [items[0][0]]
    for (prev_idx, prev_seg), (cur_idx, cur_seg) in zip(items, items[1:]):
        if cur_seg.start - prev_seg.end <= max_gap:
            acc.append(cur_idx)
        else:
            groups.append(_materialize_group(acc, segments, roles))
            acc = [cur_idx]
    groups.append(_materialize_group(acc, segments, roles))
    return groups

def _materialize_group(idxs: List[int], segments: List[Segment], roles: List[str]) -> Dict[str, Any]:
    start = segments[idxs[0]].start
    end = segments[idxs[-1]].end
    role_set = sorted(set(roles[i] for i in idxs))
    if role_set == ["DIRECTOR"] or ("DIRECTOR" in role_set and "HOST" not in role_set):
        label = "DIRECTOR_CHATTER"
    elif "HOST" not in role_set:
        label = "BETWEEN_TAKES"
    else:
        label = "MIXED"
    return {
        "group_id": f"g{idxs[0]}_{idxs[-1]}",
        "start": float(start),
        "end": float(end),
        "roles": role_set,
        "label": label,
        "segment_ids": [segments[i].id for i in idxs],
    }

def find_triplets(segments: List[Segment], roles: List[str], window_s: float = 8.0, max_seg_dur: float = 3.5) -> List[Dict[str, Any]]:
    triplets: List[Dict[str, Any]] = []
    n = len(segments)
    for i in range(n - 2):
        s1, s2, s3 = segments[i], segments[i + 1], segments[i + 2]
        r1, r2, r3 = roles[i], roles[i + 1], roles[i + 2]
        if any(s.duration > max_seg_dur for s in (s1, s2, s3)):
            continue
        if (s3.end - s1.start) > window_s:
            continue
        pattern = None
        if r1 == "DIRECTOR" and r2 == "HOST" and r3 == "DIRECTOR":
            pattern = "D-H-D"
        elif r1 == "HOST" and r2 == "DIRECTOR" and r3 == "HOST":
            pattern = "H-D-H"
        elif r1 == "DIRECTOR" and r2 == "STAFF" and r3 == "DIRECTOR":
            pattern = "D-S-D"
        elif r1 == "STAFF" and r2 == "DIRECTOR" and r3 == "STAFF":
            pattern = "S-D-S"
        if pattern:
            triplets.append({
                "pattern": pattern,
                "start": float(s1.start),
                "end": float(s3.end),
                "segment_ids": [s1.id, s2.id, s3.id],
            })
    return triplets


# ---------------------------------
# Scene Processing
# ---------------------------------

def parse_scene_segments(scene: Dict[str, Any]) -> List[Segment]:
    out: List[Segment] = []
    for seg in scene.get("timed_transcript", []) or []:
        af = AudioFeatures.from_dict(seg.get("audio_features") or {})
        role_hint = seg.get("role_hint") or af.role_hint
        try:
            s = Segment(
                id=seg.get("id"),
                start=float(seg["start"]),
                end=float(seg["end"]),
                text=str(seg.get("text", "")),
                audio=af,
                role_hint=role_hint,
            )
            out.append(s)
        except Exception:
            # skip malformed segment
            continue
    out.sort(key=lambda x: (x.start, x.end))
    return out

def update_scene_with_results(scene: Dict[str, Any],
                              segments: List[Segment],
                              roles: List[str],
                              confidences: List[float],
                              groups: List[Dict[str, Any]],
                              triplets: List[Dict[str, Any]],
                              params: Dict[str, Any]) -> Dict[str, Any]:
    # Map segment index by (id,start,end) to avoid id collisions
    key_map = {}
    for i, s in enumerate(segments):
        key_map[(s.id, s.start, s.end)] = (roles[i], confidences[i])

    new_scene = dict(scene)
    new_tt: List[Dict[str, Any]] = []
    for seg in scene.get("timed_transcript", []) or []:
        try:
            key = (seg.get("id"), float(seg["start"]), float(seg["end"]))
        except Exception:
            new_tt.append(seg)
            continue
        role, conf = key_map.get(key, ("UNKNOWN", 0.3))
        seg_out = dict(seg)
        seg_out["predicted_role_stage1"] = role
        seg_out["role_confidence_stage1"] = conf
        new_tt.append(seg_out)

    new_scene["timed_transcript"] = new_tt
    # Scene-level container for diagnostics
    new_scene["role_triplet_stage1"] = {
        "summary": {
            "segments_total": len(segments),
            "role_counts": {
                "HOST": sum(1 for r in roles if r == "HOST"),
                "DIRECTOR": sum(1 for r in roles if r == "DIRECTOR"),
                "STAFF": sum(1 for r in roles if r == "STAFF"),
                "UNKNOWN": sum(1 for r in roles if r == "UNKNOWN"),
            },
            "triplet_count": len(triplets),
            "groups_count": len(groups),
            "timeline_seconds": {
                "total": float(segments[-1].end - segments[0].start) if segments else 0.0,
                "non_host": float(sum((segments[i].end - segments[i].start) for i, r in enumerate(roles) if r != "HOST")),
            },
        },
        "groups": groups,
        "triplets": triplets,
        "params": params,
    }
    return new_scene


# ---------------------------------
# Orchestrator
# ---------------------------------

def process_scene(scene: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    segs = parse_scene_segments(scene)
    if not segs:
        # Nothing to do; pass through with placeholder container
        passthrough = dict(scene)
        passthrough["role_triplet_stage1"] = {
            "summary": {"segments_total": 0, "role_counts": {"HOST":0,"DIRECTOR":0,"STAFF":0,"UNKNOWN":0}, "triplet_count":0, "groups_count":0, "timeline_seconds":{"total":0.0,"non_host":0.0}},
            "groups": [],
            "triplets": [],
            "params": cfg,
        }
        return passthrough

    host_center, seed_idx, host_thresh = build_host_profile(segs)
    roles, confs = assign_roles(segs, host_center, host_thresh)
    groups = group_non_host_blocks(segs, roles, max_gap=float(cfg["group_max_gap_s"]))
    triplets = find_triplets(segs, roles, window_s=float(cfg["triplet_window_s"]), max_seg_dur=float(cfg["triplet_max_seg_dur_s"]))

    params = {
        "host_distance_threshold": host_thresh,
        "seed_indices": seed_idx,
        "triplet_window_s": cfg["triplet_window_s"],
        "triplet_max_seg_dur_s": cfg["triplet_max_seg_dur_s"],
        "group_max_gap_s": cfg["group_max_gap_s"],
    }
    return update_scene_with_results(scene, segs, roles, confs, groups, triplets, params)


def backup_then_write(manifest_path: Path, scenes: List[Dict[str, Any]]) -> Path:
    # Compute backup name: <stem>_stage1<suffix>, but preserve original naming if it exactly matches the known file
    stem = manifest_path.stem
    suffix = manifest_path.suffix or ".json"
    if stem.endswith("_enriched"):
        backup_name = stem.replace("_enriched", "_enriched_stage1") + suffix
    elif stem.endswith("_transcribed_enriched"):
        backup_name = stem.replace("_transcribed_enriched", "_transcribed_enriched_stage1") + suffix
    else:
        backup_name = stem + "_stage1" + suffix
    backup_path = manifest_path.with_name(backup_name)

    # Write backup
    backup_path.write_text(json.dumps(load_json(manifest_path), indent=2, ensure_ascii=False), encoding="utf-8")
    # Overwrite original with updated scenes
    manifest_path.write_text(json.dumps(scenes, indent=2, ensure_ascii=False), encoding="utf-8")
    return backup_path


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def run(manifest_path: Path,
        triplet_window_s: float,
        triplet_max_seg_dur_s: float,
        group_max_gap_s: float) -> Tuple[List[Dict[str, Any]], Path]:
    data = load_json(manifest_path)
    if not isinstance(data, list):
        raise ValueError("Manifest must be a JSON list of scenes.")
    cfg = {
        "triplet_window_s": float(triplet_window_s),
        "triplet_max_seg_dur_s": float(triplet_max_seg_dur_s),
        "group_max_gap_s": float(group_max_gap_s),
    }
    out_scenes: List[Dict[str, Any]] = []
    for scene in data:
        try:
            out_scenes.append(process_scene(scene, cfg))
        except Exception as e:
            # Pass-through on error, but record it
            fallback = dict(scene)
            fallback["role_triplet_stage1_error"] = f"{type(e).__name__}: {e}"
            out_scenes.append(fallback)
    backup_path = backup_then_write(manifest_path, out_scenes)
    return out_scenes, backup_path


# ---------------------------------
# CLI
# ---------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage-1 role labeling & triplet detection over an enriched scene manifest.")
    p.add_argument("--manifest", type=Path, default=Path("scene_transcribed_enriched.json"), help="Path to enriched manifest JSON to update in place.")
    p.add_argument("--triplet-window-s", type=float, default=8.0, help="Max window seconds to consider D/H/S alternations a triplet.")
    p.add_argument("--triplet-max-seg-dur-s", type=float, default=3.5, help="Max per-segment duration to be part of a triplet.")
    p.add_argument("--group-max-gap-s", type=float, default=1.0, help="Max silence between non-HOST segments to fuse into one chatter group.")
    return p.parse_args()


def main():
    args = parse_args()
    manifest_path: Path = args.manifest
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    out_scenes, backup_path = run(
        manifest_path=manifest_path,
        triplet_window_s=args.triplet_window_s,
        triplet_max_seg_dur_s=args.triplet_max_seg_dur_s,
        group_max_gap_s=args.group_max_gap_s,
    )
    print(f"[+] Processed scenes: {len(out_scenes)}")
    print(f"[+] Backup written: {backup_path}")
    print(f"[+] Updated manifest in place: {manifest_path}")


if __name__ == "__main__":
    main()

# Changelog
# 1.0.0 (2025-08-23)
# - New pipeline-ready stage that reads scene_transcribed_enriched.json, writes backup *_stage1.json,
#   and replaces the original with predicted roles, groups, triplets, and per-scene summaries.
# - No external dependencies; per-scene learning of HOST profile with audio-first logic.
