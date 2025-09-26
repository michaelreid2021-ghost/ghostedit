#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
File: 04a_waveform_annotator.py
Version: 1.0.0
Last Modified: 2025-08-18
Purpose: Enrich a scene transcript manifest with waveform-derived features to aid editing decisions:
         - Energy (RMS) peaks, voiced/silence spans, pitch contour (rising/flat/falling)
         - Directness proxy (spectral flatness/centroid), stereo balance (optional)
         - Role hints (host vs director) using audio + lexicon cues
         - Silence_before/silence_after near segment boundaries (for J/L cuts)
         Designed to complement Whisper/WhisperX transcripts and "clean in/out" trimming.

Notes / Rationale:
- Compute once per scene from WAV, then summarize per transcript segment (fast, deterministic).
- Works cross-platform; heavy GPU not required. Apple Silicon/Metal not needed; CPU is fine here.
- Switches let you trade speed vs fidelity and adapt to different houses (Mac Pro/Ultra vs prototype desktops).
- Lexicon is loaded from a per-project lexicon.json (future-proof to encode director styles and slate phrases).

Inputs:
  - A manifest JSON (from your previous transcription step) with scenes and `timed_transcript` segments.
  - Proxy videos to extract audio from (or point directly to WAVs).

Outputs:
  - A new manifest JSON with each segment annotated under `audio_features`, plus optional scene-level peak markers.

CLI examples:
  python 4a_waveform_annotator.py --proxies X:/proxies --manifest_in scene_manifest_transcribed.json --out scene_manifest_enriched.json
  python 4a_waveform_annotator.py --stereo --vad webrtc --vad_aggr 2 --lexicon lexicon.json

Dependencies:
  numpy, librosa, soundfile, scipy, (optional) webrtcvad
"""

import os
import re
import gc
import json
import math
import argparse
import subprocess
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

import numpy as np

# Audio processing deps
import librosa
import soundfile as sf
from scipy.signal import find_peaks

# Optional: robust VAD if available; else we fall back to RMS-based VAD
try:
    import webrtcvad
    HAVE_WEBRTCVAD = True
except Exception:
    HAVE_WEBRTCVAD = False

# Project paths integration (optional)
try:
    from project_paths import add_base_arg, from_args  # type: ignore
    HAVE_PROJECT_PATHS = True
except Exception:
    HAVE_PROJECT_PATHS = False


# ----------------------------
# Defaults & Tunables
# ----------------------------
DEFAULT_SR_MAIN = 16000        # core analysis rate; webrtcvad expects 8k/16k/32k/48k (we use 16k)
PITCH_SR = 22050               # librosa.pyin prefers 22.05 kHz
FRAME_MS = 25                  # analysis window
HOP_MS = 10                    # analysis hop
RMS_SMOOTH_HOPS = 3            # post-smoothing of RMS to reduce flicker

# Peak detection
PEAK_PROMINENCE = 0.8          # relative to scene RMS median; tweakable
PEAK_DISTANCE_S = 0.35         # min distance between peaks (seconds)

# Silence windows (for boundary context)
SIL_BEFORE_WINDOW = 1.0        # seconds before segment start to measure silence
SIL_AFTER_WINDOW  = 1.0        # seconds after  segment end   to measure silence

# Role / directness heuristics
DIRECTNESS_W_FLAT = 0.65       # weight of (1 - spectral_flatness)
DIRECTNESS_W_CENT = 0.35       # weight of normalized spectral centroid
DIRECTNESS_MIN_PEAK_CENT = 2000.0  # Hz threshold for "crispness" normalization

# Pitch bounds (human speech)
FMIN_HZ = 81.0
FMAX_HZ = 300.0

# Lexicon defaults
DEFAULT_LEXICON = {
    "lead_in": [
        "we are rolling", "ready to roll", "rolling", "camera speed", "speed",
        "mark it", "marker", "slate", "sticks", "scene", "take", "sound speed",
        "coming in three", "coming in 3", "three, two", "three two", "two, one", "3, 2, 1", "3 2 1", "3 2", "in 3 2", "3, 2", "in 3, 2",
    ],
    "chatter": [
        "all right", "alright", "okay", "ok", "very nice", "here we go", "let's go", "yep", "yeah",
        "mm-hmm", "uh-huh", "ready", "set", "cut", "reset", "again", "from the top", "let me retry", "do that again",
    ],
    "clapperboard": [
        "scene a1", "scene b1", "take one", "take two", "take 1", "take 2",
    ],
    "outro": [
        "cut", "that's a wrap", "wrap", "we're good", "we are good", "stop", "thank you", "thanks everyone", "well done", "good job",
    ],
    "proper_nouns": [
        "Scott", "Paris", "Smith", "Jones", "John", "Jane", "Bass Pro", "Cabela's",
    ],
}

# ----------------------------
# Utility
# ----------------------------
def resolve_ffmpeg(ffmpeg_path: str) -> Optional[str]:
    p = Path(ffmpeg_path)
    if p.exists():
        return str(p)
    if ffmpeg_path.lower() == "ffmpeg":
        return "ffmpeg"
    return None


def extract_audio(ffmpeg_bin: str, video_path: Path, wav_out: Path, sr: int, stereo: bool) -> None:
    args = [
        ffmpeg_bin, "-y", "-i", str(video_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", str(sr),
        "-ac", "2" if stereo else "1",
        str(wav_out),
    ]
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def load_lexicon(path: Optional[Path]) -> Dict[str, List[str]]:
    if path and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # ensure all keys exist
            for k, v in DEFAULT_LEXICON.items():
                if k not in data or not isinstance(data[k], list):
                    data[k] = v
            return data
        except Exception:
            pass
    return DEFAULT_LEXICON.copy()


def build_regex_groups(lex: Dict[str, List[str]]) -> Dict[str, re.Pattern]:
    def norm(s: str) -> str:
        return re.escape(s.lower())
    groups = {}
    for key in ["lead_in", "chatter", "clapperboard", "outro"]:
        toks = [norm(t) for t in lex.get(key, []) if t.strip()]
        groups[key] = re.compile(r"\b(" + "|".join(toks) + r")\b", re.IGNORECASE) if toks else re.compile(r"$^\b$")
    return groups


def initial_prompt_from_lexicon(lex: Dict[str, List[str]]) -> str:
    seeds = set(lex.get("clapperboard", []) + lex.get("proper_nouns", []))
    return ", ".join(sorted(seeds))


def frame_length_samples(sr: int, ms: int) -> int:
    return max(1, int(round(sr * (ms / 1000.0))))


def times_for_frames(n_frames: int, hop_length: int, sr: int) -> np.ndarray:
    return (np.arange(n_frames) * hop_length) / float(sr)


def zscore(x: np.ndarray) -> np.ndarray:
    mu = np.nanmean(x)
    sd = np.nanstd(x) + 1e-12
    return (x - mu) / sd


def robust_quantiles(x: np.ndarray, qs=(0.25, 0.5, 0.75, 0.95)) -> Dict[str, float]:
    vals = np.quantile(x[~np.isnan(x)], qs).tolist() if np.any(~np.isnan(x)) else [float("nan")] * len(qs)
    return {f"q{int(q*100)}": v for q, v in zip(qs, vals)}


# ----------------------------
# Feature Extraction
# ----------------------------
def compute_features(y: np.ndarray, sr: int, stereo: bool, frame_ms: int, hop_ms: int) -> Dict[str, Any]:
    """
    Compute per-frame features: RMS, ZCR, spectral centroid, spectral flatness.
    Returns also the time axis and stereo balance (if stereo).
    """
    # If stereo: y shape (2, n); compute mono for core features; retain L/R for balance
    if stereo and y.ndim == 2 and y.shape[0] == 2:
        y_mono = np.mean(y, axis=0)
        y_l, y_r = y[0], y[1]
    else:
        y_mono = y if y.ndim == 1 else np.mean(y, axis=0)
        y_l, y_r = None, None
        stereo = False

    frame_len = frame_length_samples(sr, frame_ms)
    hop_len = frame_length_samples(sr, hop_ms)

    # RMS
    rms = librosa.feature.rms(y=y_mono, frame_length=frame_len, hop_length=hop_len, center=True, pad_mode="reflect").flatten()
    # Smooth RMS a bit
    if RMS_SMOOTH_HOPS > 0:
        kernel = np.ones(RMS_SMOOTH_HOPS) / RMS_SMOOTH_HOPS
        rms = np.convolve(rms, kernel, mode="same")

    # ZCR
    zcr = librosa.feature.zero_crossing_rate(y=y_mono, frame_length=frame_len, hop_length=hop_len, center=True).flatten()

    # Spectral features
    S = np.abs(librosa.stft(y_mono, n_fft=2048, hop_length=hop_len, win_length=frame_len, center=True))
    cent = librosa.feature.spectral_centroid(S=S, sr=sr, hop_length=hop_len).flatten()
    flat = librosa.feature.spectral_flatness(S=S).flatten()

    # Times
    t = times_for_frames(len(rms), hop_len, sr)

    # Stereo balance (RMS L/R)
    balance = None
    if stereo:
        # Compute per-frame RMS for each channel
        rms_l = librosa.feature.rms(y=y_l, frame_length=frame_len, hop_length=hop_len, center=True, pad_mode="reflect").flatten()
        rms_r = librosa.feature.rms(y=y_r, frame_length=frame_len, hop_length=hop_len, center=True, pad_mode="reflect").flatten()
        balance = np.clip((rms_l - rms_r) / (rms_l + rms_r + 1e-9), -1.0, 1.0)  # -1=right, +1=left

    return {"t": t, "rms": rms, "zcr": zcr, "centroid": cent, "flatness": flat, "balance": balance, "sr": sr, "hop_len": hop_len}


def compute_pitch(y: np.ndarray, sr: int, frame_ms: int, hop_ms: int) -> Tuple[np.ndarray, np.ndarray]:
    """Pitch via librosa.pyin; returns times and f0 array (Hz with NaN where unvoiced)."""
    frame_len = frame_length_samples(sr, frame_ms)
    hop_len = frame_length_samples(sr, hop_ms)
    # pyin expects float32
    y = librosa.to_mono(y) if y.ndim > 1 else y
    y = y.astype(np.float32)
    f0, _, _ = librosa.pyin(y, frame_length=frame_len, hop_length=hop_len, fmin=FMIN_HZ, fmax=FMAX_HZ, sr=sr)
    t = times_for_frames(len(f0), hop_len, sr)
    return t, f0


def detect_voiced_webrtc(y: np.ndarray, sr: int, hop_ms: int, aggressiveness: int) -> Tuple[np.ndarray, np.ndarray]:
    """
    VAD via webrtcvad over fixed-size frames (must be 10/20/30 ms). We'll use hop_ms and 30ms window.
    Returns frame times and boolean voiced mask.
    """
    vad = webrtcvad.Vad(max(0, min(3, aggressiveness)))
    frame_dur_ms = 30
    frame_len = frame_length_samples(sr, frame_dur_ms)
    hop_len = frame_length_samples(sr, hop_ms)
    # Ensure 16-bit PCM for VAD
    y16 = np.clip(y * 32768.0, -32768, 32767).astype(np.int16)  # mono expected
    bytes_data = y16.tobytes()

    voiced = []
    times = []
    for start in range(0, len(y16) - frame_len + 1, hop_len):
        end = start + frame_len
        frame_bytes = bytes_data[start * 2:end * 2]
        is_voiced = vad.is_speech(frame_bytes, sr)
        voiced.append(is_voiced)
        times.append(start / sr)
    return np.array(times, dtype=float), np.array(voiced, dtype=bool)


def detect_voiced_rms(rms: np.ndarray, t: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Fallback VAD using an adaptive RMS threshold (median + 0.5*mad)."""
    med = np.nanmedian(rms)
    mad = np.nanmedian(np.abs(rms - med)) + 1e-9
    th = med + 0.5 * mad
    voiced = rms > th
    return t.copy(), voiced


def energy_peaks(rms: np.ndarray, t: np.ndarray, scene_rms_median: float) -> List[float]:
    # normalize RMS relative to scene median to improve scale-robustness
    norm = rms / (scene_rms_median + 1e-12)
    distance = max(1, int(round(PEAK_DISTANCE_S / (t[1] - t[0])))) if len(t) > 1 else 1
    peaks, _ = find_peaks(norm, prominence=PEAK_PROMINENCE, distance=distance)
    return t[peaks].tolist()


def summarize_segment_window(mask: np.ndarray, arr: np.ndarray, fn="mean") -> float:
    vals = arr[mask]
    if vals.size == 0:
        return float("nan")
    if fn == "mean":
        return float(np.nanmean(vals))
    if fn == "median":
        return float(np.nanmedian(vals))
    if fn == "p95":
        return float(np.nanpercentile(vals, 95))
    if fn == "p05":
        return float(np.nanpercentile(vals, 5))
    return float(np.nanmean(vals))


def pitch_trend(times: np.ndarray, f0: np.ndarray) -> Tuple[str, float]:
    """Return 'rising'/'falling'/'flat' and slope (Hz/sec) ignoring NaNs."""
    mask = ~np.isnan(f0)
    if np.sum(mask) < 3:
        return "flat", 0.0
    x = times[mask]
    y = f0[mask]
    # simple linear fit
    A = np.vstack([x, np.ones_like(x)]).T
    slope, _ = np.linalg.lstsq(A, y, rcond=None)[0]
    label = "rising" if slope > 1.0 else ("falling" if slope < -1.0 else "flat")
    return label, float(slope)


def directness_score(flatness: np.ndarray, centroid: np.ndarray) -> float:
    """Heuristic: higher when spectrum is peaky and centroid reasonably high."""
    # Normalize centroid
    c_norm = np.clip(np.nanmean(centroid) / max(DIRECTNESS_MIN_PEAK_CENT, 1.0), 0.0, 1.5)
    f_term = 1.0 - float(np.nanmean(flatness))
    score = max(0.0, min(1.0, DIRECTNESS_W_FLAT * f_term + DIRECTNESS_W_CENT * c_norm))
    return float(score)


def stereo_balance_ratio(balance: Optional[np.ndarray], mask: np.ndarray) -> float:
    """Mean absolute balance within segment (0=even L/R, 1=all-left or all-right)."""
    if balance is None:
        return float("nan")
    vals = balance[mask]
    if vals.size == 0:
        return float("nan")
    return float(np.nanmean(np.abs(vals)))


def silence_around_segment(vad_t: np.ndarray, vad_voiced: np.ndarray, start_s: float, end_s: float,
                           before_window: float, after_window: float) -> Tuple[float, float]:
    """Compute leading and trailing silence durations using VAD mask."""
    # Before
    before_mask = (vad_t >= max(0.0, start_s - before_window)) & (vad_t < start_s)
    after_mask  = (vad_t > end_s) & (vad_t <= end_s + after_window)

    def run_silence(mask: np.ndarray, reverse: bool) -> float:
        idx = np.where(mask)[0]
        if idx.size == 0:
            return 0.0
        sub_vad = vad_voiced[idx]
        # Count consecutive unvoiced frames from the boundary inwards
        if reverse:
            sub_vad = sub_vad[::-1]
        count = 0
        for v in sub_vad:
            if v:
                break
            count += 1
        # time per VAD hop ~ difference between consecutive vad_t indices
        # Assume uniform; derive from neighbor spacing if possible
        hop_s = np.median(np.diff(vad_t)) if vad_t.size > 1 else 0.01
        return float(count * hop_s)

    sil_before = run_silence(before_mask, reverse=True)
    sil_after  = run_silence(after_mask, reverse=False)
    return sil_before, sil_after


def label_energy_band(scene_rms: np.ndarray, seg_rms_mean: float) -> str:
    q = robust_quantiles(scene_rms, qs=(0.33, 0.66))
    if np.isnan(seg_rms_mean):
        return "unknown"
    if seg_rms_mean <= q["q33"]:
        return "low"
    if seg_rms_mean <= q["q66"]:
        return "medium"
    return "high"


def role_hint_from_features(text: str,
                            energy_band: str,
                            directness: float,
                            voiced_frac: float,
                            stereo_balance_abs: float,
                            regex_groups: Dict[str, re.Pattern]) -> str:
    t = (text or "").lower()
    # Strong lexical cues first
    if regex_groups["lead_in"].search(t) or regex_groups["clapperboard"].search(t):
        return "director_cue"
    if regex_groups["chatter"].search(t):
        return "director_chatter"

    # Audio heuristics
    # Host: higher energy, higher directness, strongly voiced
    if energy_band == "high" and directness > 0.6 and voiced_frac > 0.7:
        return "host"

    # Director/off-camera: lower directness and/or lower energy, balanced stereo (room)
    if energy_band in ("low", "medium") and directness < 0.45 and voiced_frac < 0.8:
        return "director_like"

    # If stereo highly imbalanced, might be a boom swing or single-channel artifact; avoid over-labeling
    if not math.isnan(stereo_balance_abs) and stereo_balance_abs < 0.15 and directness < 0.5:
        return "off_camera_like"

    return "unknown"


# ----------------------------
# Main Pipeline
# ----------------------------
def annotate_scene(scene: Dict[str, Any],
                   proxies_dir: Path,
                   ffmpeg_bin: str,
                   lexicon: Dict[str, List[str]],
                   stereo: bool,
                   vad_mode: str,
                   vad_aggr: int,
                   keep_audio: bool) -> Dict[str, Any]:
    proxy_filename = scene.get("proxy_filename")
    scene_number = scene.get("scene_number", "")
    video_path = proxies_dir / proxy_filename
    if not video_path.exists():
        raise FileNotFoundError(f"Proxy not found: {video_path}")

    # Extract WAV (stereo optional for balance analysis)
    temp_dir = Path("temp_audio")
    temp_dir.mkdir(parents=True, exist_ok=True)
    wav_path = temp_dir / f"scene_{scene_number}_waveform.wav"
    extract_audio(ffmpeg_bin, video_path, wav_path, sr=DEFAULT_SR_MAIN, stereo=stereo)

    # Load audio
    y, sr = sf.read(str(wav_path), always_2d=True)  # shape (n_samples, n_channels)
    y = y.T  # (channels, samples)
    # Normalize to mono/stereo arrays in float32
    if y.shape[0] == 1:
        y_mono = y[0].astype(np.float32)
        y_stereo = False
        y_arr = y_mono
    else:
        y_l = y[0].astype(np.float32)
        y_r = y[1].astype(np.float32)
        y_mono = 0.5 * (y_l + y_r)
        y_stereo = True
        y_arr = np.vstack([y_l, y_r])

    # Per-frame features at DEFAULT_SR_MAIN
    feats = compute_features(y_arr, sr=sr, stereo=y_stereo, frame_ms=FRAME_MS, hop_ms=HOP_MS)
    t = feats["t"]; rms = feats["rms"]; zcr = feats["zcr"]; cent = feats["centroid"]; flat = feats["flatness"]; balance = feats["balance"]
    scene_rms_median = float(np.nanmedian(rms))

    # Pitch at 22.05k (resample mono)
    y_p, _ = librosa.load(sf.SoundFile(str(wav_path)), sr=PITCH_SR, mono=True)
    pt, f0 = compute_pitch(y_p, PITCH_SR, FRAME_MS, HOP_MS)

    # VAD
    if vad_mode == "webrtc" and HAVE_WEBRTCVAD and sr in (8000, 16000, 32000, 48000):
        vad_t, vad_voiced = detect_voiced_webrtc(y_mono, sr, hop_ms=HOP_MS, aggressiveness=vad_aggr)
    else:
        # fallback to RMS-threshold VAD using analysis frames
        vad_t, vad_voiced = t, detect_voiced_rms(rms, t)[1]

    # Scene-level energy peaks (useful for global markers)
    peaks = energy_peaks(rms, t, scene_rms_median)

    # Regex groups from lexicon
    regex_groups = build_regex_groups(lexicon)

    # Annotate segments
    segments = scene.get("timed_transcript", []) or []
    new_segments: List[Dict[str, Any]] = []
    for seg in segments:
        s = float(seg.get("start", 0.0))
        e = float(seg.get("end", s))
        text = (seg.get("text") or "").strip()

        # Masks for analysis arrays
        mask_main = (t >= s) & (t <= e)
        mask_pitch = (pt >= s) & (pt <= e)
        mask_vad = (vad_t >= s) & (vad_t <= e)

        # Summaries
        seg_rms_mean = summarize_segment_window(mask_main, rms, "mean")
        seg_rms_p95  = summarize_segment_window(mask_main, rms, "p95")
        seg_zcr_mean = summarize_segment_window(mask_main, zcr, "mean")
        seg_cent_mean = summarize_segment_window(mask_main, cent, "mean")
        seg_flat_mean = summarize_segment_window(mask_main, flat, "mean")
        seg_directness = directness_score(flat[mask_main], cent[mask_main])
        seg_balance = stereo_balance_ratio(balance, mask_main) if balance is not None else float("nan")

        # Pitch
        f0_seg = f0[mask_pitch]
        pitch_label, pitch_slope = pitch_trend(pt[mask_pitch], f0_seg)
        pitch_mean = float(np.nanmean(f0_seg)) if np.any(~np.isnan(f0_seg)) else float("nan")

        # Voiced fraction
        voiced_frac = float(np.mean(vad_voiced[mask_vad])) if np.any(mask_vad) else 0.0

        # Silence around boundaries
        sil_before, sil_after = silence_around_segment(vad_t, vad_voiced, s, e, SIL_BEFORE_WINDOW, SIL_AFTER_WINDOW)

        # Energy band
        eband = label_energy_band(rms, seg_rms_mean)

        # Role hint
        role_hint = role_hint_from_features(text, eband, seg_directness, voiced_frac, seg_balance, regex_groups)

        # Peak count inside segment
        # Use scene peak list for consistency
        peak_count = sum(1 for p in peaks if (p >= s and p <= e))

        # Attach features
        seg_out = dict(seg)
        seg_out["audio_features"] = {
            "energy_mean": round(seg_rms_mean, 6),
            "energy_p95": round(seg_rms_p95, 6),
            "energy_band": eband,
            "zcr_mean": round(seg_zcr_mean, 6),
            "centroid_mean_hz": round(seg_cent_mean, 2),
            "flatness_mean": round(seg_flat_mean, 6),
            "directness_score": round(seg_directness, 3),     # 0..1 (higher ~ closer/directer)
            "stereo_balance_abs": (round(seg_balance, 3) if not math.isnan(seg_balance) else None),  # 0 even, 1 single-side
            "pitch_mean_hz": (round(pitch_mean, 2) if not math.isnan(pitch_mean) else None),
            "pitch_trend": pitch_label,                      # rising/flat/falling
            "pitch_slope_hz_per_s": round(pitch_slope, 3),
            "voiced_fraction": round(voiced_frac, 3),
            "silence_before_s": round(sil_before, 3),
            "silence_after_s": round(sil_after, 3),
            "energy_peak_count": int(peak_count),
            "role_hint": role_hint,
        }
        new_segments.append(seg_out)

    # Scene-level markers
    new_scene = dict(scene)
    new_scene["timed_transcript"] = new_segments
    new_scene["energy_peak_markers"] = [{"t": round(p, 3), "type": "energy_peak"} for p in peaks]

    # Cleanup temp
    if not keep_audio:
        try:
            wav_path.unlink()
        except Exception:
            pass

    return new_scene


def cli():
    parser = argparse.ArgumentParser(description="Annotate transcript segments with waveform-derived features for edit decisions.")
    if HAVE_PROJECT_PATHS:
        add_base_arg(parser)  # project base integration if available

    parser.add_argument("--proxies", type=Path, default=None, help="Directory containing proxy .mp4 files. Defaults to project_paths.proxies when available.")
    parser.add_argument("--manifest_in", type=Path, default=None, help="Input manifest JSON with `timed_transcript` (default: project_paths.scene_manifest or ./scene_manifest_transcribed.json).")
    parser.add_argument("--out", type=Path, default=None, help="Output manifest path (default: project_paths.scene_manifest_enriched or ./scene_manifest_enriched.json).")
    parser.add_argument("--ffmpeg", type=str, default=r"C:\ffmpeg\bin\ffmpeg.exe", help="Path to ffmpeg (or 'ffmpeg' if on PATH).")

    # Analysis switches
    parser.add_argument("--stereo", action="store_true", help="Extract stereo for balance analysis (default mono).")
    parser.add_argument("--vad", type=str, choices=["webrtc", "rms"], default="webrtc", help="VAD backend: webrtc (if available) or rms.")
    parser.add_argument("--vad_aggr", type=int, default=2, help="WebRTC VAD aggressiveness 0..3 (higher=more speech filtering).")
    parser.add_argument("--lexicon", type=Path, default=Path("lexicon.json"), help="Per-project lexicon.json (director styles, slate phrases, etc.).")
    parser.add_argument("--keep_audio", action="store_true", help="Keep extracted wavs in temp_audio.")
    parser.add_argument("--sr", type=int, default=DEFAULT_SR_MAIN, help="Sample rate for analysis/extraction (16k recommended).")

    parser.add_argument("--ffmpeg-dir", default=r"C:\ffmpeg\bin", type=Path, help="Path to the directory containing ffmpeg and ffprobe.")
    args = parser.parse_args()

    # Resolve project paths if available
    if HAVE_PROJECT_PATHS:
        p = from_args(args)
        proxies_dir = args.proxies or p.proxies
        manifest_in = args.manifest_in or (p.scene_manifest if (p.scene_manifest and Path(p.scene_manifest).exists()) else Path("scene_manifest_transcribed.json"))
        out_path = args.out or (getattr(p, "scene_manifest_enriched", None) or Path("scene_manifest_enriched.json"))
    else:
        proxies_dir = args.proxies or Path(".")
        manifest_in = args.manifest_in or Path("scene_manifest_transcribed.json")
        out_path = args.out or Path("scene_manifest_enriched.json")

    # ffmpeg
    ffmpeg_bin = resolve_ffmpeg(args.ffmpeg)
    if not ffmpeg_bin:
        print(f"[!] ffmpeg not found: {args.ffmpeg}")
        return

    # Validate inputs
    if not manifest_in.exists():
        print(f"[!] Input manifest not found: {manifest_in}")
        return
    if not proxies_dir.exists():
        print(f"[!] Proxies directory not found: {proxies_dir}")
        return

    # Load input manifest
    scenes = json.loads(manifest_in.read_text(encoding="utf-8"))
    print(f"[+] Loaded manifest: {manifest_in} | scenes: {len(scenes)}")

    # Load lexicon
    lexicon = load_lexicon(args.lexicon)
    print(f"[+] Lexicon loaded with categories: {', '.join(sorted(lexicon.keys()))}")

    # Warn if webrtc requested but unavailable
    vad_mode = args.vad
    if vad_mode == "webrtc" and not HAVE_WEBRTCVAD:
        print("[!] webrtcvad not installed; falling back to RMS VAD.")
        vad_mode = "rms"

    # Process scenes
    out_scenes: List[Dict[str, Any]] = []
    for i, scene in enumerate(scenes, 1):
        try:
            proxy_filename = scene.get("proxy_filename", "")
            print(f"({i}/{len(scenes)}) Annotating: {proxy_filename}")
            enriched = annotate_scene(scene, proxies_dir, ffmpeg_bin, lexicon, args.stereo, vad_mode, args.vad_aggr, args.keep_audio)
            out_scenes.append(enriched)
            # periodic GC to keep memory tame on modest rigs
            if i % 4 == 0:
                gc.collect()
        except Exception as e:
            print(f"    [!] ERROR {type(e).__name__}: {e}")
            # pass through scene unchanged with a marker
            fallback = dict(scene)
            fallback["audio_features_error"] = f"{type(e).__name__}: {e}"
            out_scenes.append(fallback)

    # Save
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out_scenes, indent=2), encoding="utf-8")
    print(f"[+] Saved enriched manifest -> {out_path}")


if __name__ == "__main__":
    cli()

# Changelog / Developer Notes
# 1.0.0 (2025-08-18):
# - Initial release. Per-frame RMS/ZCR/centroid/flatness, pyin pitch, optional WebRTC VAD,
#   stereo balance (optional), energy peak markers, boundary silence, directness score, role hints.
# - Lexicon.json support (future-proof for director style phrases). Seeds can be reused for transcription initial_prompt.
# - Tunable switches to adapt to high-end Mac rigs vs modest prototype desktops without changing code.
# TODO:
# - Optional CREPE pitch backend (GPU/Metal) for noisy rooms.
# - Export NLE markers (FCPXML/DaVinci) for energy peaks and director cues.
# - Learn role classifier from a small labeled set to replace heuristics.
