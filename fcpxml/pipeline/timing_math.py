# timing_math.py
from fractions import Fraction
from typing import Dict, Optional, Tuple

# ---------- helpers ----------
def _parse_fraction(s: str) -> Optional[Fraction]:
    if not s or s == "0/0": return None
    n, d = s.split("/")
    d = int(d); n = int(n)
    if d == 0: return None
    return Fraction(n, d)

def _fps_from(fr: Optional[Fraction]) -> Optional[float]:
    return float(fr) if fr else None

def _canonical_timebase(fps: float) -> Tuple[int, int, int]:
    """Return (tb_num, tb_den, nominal_fps) where tb_num/tb_den = frames/sec."""
    KNOWN = [
        (24000, 1001, 24),  # 23.976
        (30000, 1001, 30),  # 29.97
        (60000, 1001, 60),  # 59.94
        (24, 1, 24),
        (25, 1, 25),
        (30, 1, 30),
        (50, 1, 50),
        (60, 1, 60),
    ]
    def close(a,b,eps): return abs(a-b) < eps
    for n,d,nom in KNOWN:
        if close(fps, n/d, 1e-3):
            return n, d, nom
    # last resort: rationalize
    frac = Fraction(fps).limit_denominator(1001)
    nom  = int(round(fps))
    return frac.numerator, frac.denominator, nom

def _tc_is_dropframe(tc: str) -> bool:
    # SMPTE drop-frame typically uses semicolon or comma before frames: HH:MM:SS;FF
    return ";" in tc or "," in tc

def _tc_to_frames(tc: str, nominal_fps: int, drop: bool) -> int:
    # Accept HH:MM:SS:FF (NDF) or HH:MM:SS;FF (DF)
    tc = tc.strip()
    sep = ";" if ";" in tc else ":"
    hh, mm, ss, ff = tc.replace(";", ":").split(":")
    hh = int(hh); mm = int(mm); ss = int(ss); ff = int(ff)

    frames = (hh*3600 + mm*60 + ss) * nominal_fps + ff

    if drop and nominal_fps in (30, 60):
        # Drop-frame: 29.97 → 2 df per minute except every 10th minute
        # For 59.94 → 4 df per minute except every 10th minute
        drop_per_min = 2 if nominal_fps == 30 else 4
        total_minutes = 60*hh + mm
        frames -= drop_per_min * (total_minutes - total_minutes // 10)
    return frames

def _write_fcpx_seconds_unreduced(frames: int, tb_num: int, tb_den: int) -> str:
    # exact: frames * (seconds per frame) = frames * tb_den / tb_num
    num = frames * tb_den
    den = tb_num
    return f"{num}/{den}s"  # DO NOT reduce; keeps numerator multiple of tb_den

def _round_frames(fr: Fraction) -> Tuple[int, Fraction]:
    r = int(round(float(fr)))
    return r, abs(fr - r)

# ---------- public API ----------
def derive_frame_locked_timing(ffp: Dict) -> Dict:
    """
    Given an ffprobe JSON dict for a single media file, return a dict:
    {
      'tb_num','tb_den','nominal_fps','tc_format',          # 'DF' or 'NDF'
      'start_frames','duration_frames','nb_frames',
      'start_s_fcpx','duration_s_fcpx'
    }
    All outputs are frame-locked to tb_num/tb_den.
    """
    streams = ffp.get("streams", [])
    fmt     = ffp.get("format", {}) or {}
    # --- choose the best video stream ---
    vstream = next((s for s in streams if s.get("codec_type") == "video"), None)

    # 1) decide frame rate
    r_fr = _parse_fraction(vstream.get("r_frame_rate")) if vstream else None
    a_fr = _parse_fraction(vstream.get("avg_frame_rate")) if vstream else None
    fps  = _fps_from(a_fr or r_fr)
    if not fps or fps <= 0:
        # extremely rare: guess from nb_frames/duration
        nb  = vstream.get("nb_frames")
        dur = vstream.get("duration") or fmt.get("duration")
        if nb and dur:
            fps = int(nb) / float(dur)
        else:
            fps = 24.0  # safe default
    tb_num, tb_den, nominal_fps = _canonical_timebase(fps)

    # 2) duration in frames (prefer nb_frames if sane)
    nb_frames = None
    if vstream and vstream.get("nb_frames"):
        try:
            nb_frames = int(vstream["nb_frames"])
        except Exception:
            nb_frames = None
    if nb_frames is None:
        # compute from duration_ts/time_base if present; else from duration*fps
        dur_ts  = vstream.get("duration_ts")
        tbase_s = _parse_fraction(vstream.get("time_base"))
        if dur_ts is not None and tbase_s is not None:
            dur_sec = Fraction(int(dur_ts)) * tbase_s  # seconds
            frames_f = dur_sec * Fraction(tb_num, tb_den)
        else:
            dur_str = vstream.get("duration") or fmt.get("duration") or "0"
            dur_sec = Fraction(str(dur_str))  # Fraction can take decimal string
            frames_f = dur_sec * Fraction(tb_num, tb_den)
        nb_frames, _ = _round_frames(frames_f)

    # 3) start: prefer TMCD
    #    look for tmcd stream tag or container tag
    tc_str = None
    for s in streams:
        if s.get("codec_type") == "data" and s.get("codec_tag_string", "").lower() in ("tmcd","timecode"):
            tc_str = (s.get("tags") or {}).get("timecode")
            if tc_str: break
    if not tc_str:
        tc_str = (fmt.get("tags") or {}).get("timecode")

    tc_format = "NDF"
    if tc_str:
        drop = _tc_is_dropframe(tc_str)
        tc_format = "DF" if drop else "NDF"
        start_frames = _tc_to_frames(tc_str, nominal_fps, drop)
    else:
        # fallback to PTS/time_base or 0
        start_pts = vstream.get("start_pts")
        tbase_s   = _parse_fraction(vstream.get("time_base")) if vstream else None
        if start_pts is not None and tbase_s is not None:
            sec = Fraction(int(start_pts)) * tbase_s
        else:
            sec = Fraction(0,1)
        # frames (rational), then snap
        frames_f = sec * Fraction(tb_num, tb_den)
        start_frames, drift = _round_frames(frames_f)
        # sub-frame snap tolerance: <= 0.5 frame
        if drift > Fraction(1,2):
            # Violent offset → hard fail (caller will catch).
            raise ValueError(f"Start drift {float(drift)} frames is too large without TMCD.")
        # else we snapped silently

    # 4) Emit FCPXML-friendly seconds (unreduced)
    start_s = _write_fcpx_seconds_unreduced(start_frames, tb_num, tb_den)
    dur_s   = _write_fcpx_seconds_unreduced(nb_frames,     tb_num, tb_den)

    return {
        "tb_num": tb_num,
        "tb_den": tb_den,
        "nominal_fps": nominal_fps,
        "tc_format": tc_format,
        "start_frames": start_frames,
        "duration_frames": nb_frames,
        "nb_frames": nb_frames,
        "start_s_fcpx": start_s,
        "duration_s_fcpx": dur_s,
    }
