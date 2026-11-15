"""Programme loudness (ITU-R BS.1770-4 / EBU R128), true peak and gain planning, without FFmpeg.

Delivery of dialogue and mixes is judged on three numbers: integrated loudness (LUFS), loudness range
(LU) and true peak (dBTP).  This module measures them from samples and plans the one linear gain that
brings a mix to a delivery target, refusing -- or, by explicit choice, stopping short -- when that gain
would push the true peak over the ceiling.  No limiter or dynamics processing is applied, so a gain
change moves every loudness value by exactly the same number of decibels and nothing else about the
audio changes.

What is measured (all from the audio alone, deterministic):

* K-weighting: the BS.1770-4 pre-filter (high shelf) and RLB high-pass, with coefficients derived for
  the actual sample rate from the standard's analogue prototype, so any rate is exact rather than
  resampled.  At 48 kHz they equal the standard's published table.
* Integrated loudness: 400 ms blocks at 75 % overlap, absolute gate at -70 LUFS, relative gate 10 LU
  below the absolute-gated mean (BS.1770-4 section 5.1; EBU Tech 3341).
* Momentary maximum (400 ms) and short-term maximum (3 s).
* Loudness range (EBU Tech 3342): 3 s short-term values every 100 ms, absolute gate -70 LUFS, relative
  gate 20 LU below the absolute-gated mean, 10th to 95th percentile spread.
* True peak: 4x oversampling below 96 kHz, 2x below 192 kHz (Annex 2 of BS.1770-4) and sample peak.

Audio is float64 with shape ``(samples,)`` or ``(samples, channels)``.  Channel weights follow the
standard: left, right and centre 1.0, surrounds 1.41, LFE excluded.  What the measurement does not say:
it is a level measurement, not a judgement that the mix sounds right, and a delivery preset below is a
commonly published default, not the requirement of any particular distributor.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.signal import lfilter, resample_poly

SCHEMA = "amps.loudness.v1"
OFFSET = -0.691                      # BS.1770-4 constant in L = -0.691 + 10 log10(sum G_i z_i)
ABSOLUTE_GATE = -70.0
RELATIVE_GATE = -10.0
LRA_RELATIVE_GATE = -20.0
BLOCK_S, HOP_S = 0.4, 0.1
SHORT_TERM_S = 3.0
SURROUND = 1.41

LAYOUTS = {
    "mono": (1.0,),
    "stereo": (1.0, 1.0),
    "5.1": (1.0, 1.0, 1.0, 0.0, SURROUND, SURROUND),     # L R C LFE Ls Rs
}

# Commonly published targets.  A distributor's own specification overrides these; pass a spec mapping.
PRESETS: dict[str, dict[str, Any]] = {
    "ebu_r128": {"name": "ebu_r128", "integrated_lufs": {"target": -23.0, "tolerance": 0.5}, "true_peak_dbtp_max": -1.0},
    "atsc_a85": {"name": "atsc_a85", "integrated_lufs": {"target": -24.0, "tolerance": 2.0}, "true_peak_dbtp_max": -2.0},
    "streaming": {"name": "streaming", "integrated_lufs": {"target": -14.0, "tolerance": 1.0}, "true_peak_dbtp_max": -1.0},
    "podcast": {"name": "podcast", "integrated_lufs": {"target": -16.0, "tolerance": 1.0}, "true_peak_dbtp_max": -1.0},
}


class LoudnessError(ValueError):
    """Raised when audio cannot be measured (shape, rate, too short) or a spec is malformed."""


def _as_2d(audio: np.ndarray) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim == 1:
        return audio[:, None]
    if audio.ndim != 2 or audio.shape[1] < 1:
        raise LoudnessError("audio must have shape (samples,) or (samples, channels)")
    if not np.isfinite(audio).all():
        raise LoudnessError("audio contains NaN or infinite samples")
    return audio


def audio_sha256(audio: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(np.asarray(audio, dtype="<f8")).tobytes()).hexdigest()


def db(value: float) -> float:
    return 20.0 * math.log10(max(float(value), 1e-12))


def channel_weights(channels: int, layout: str | Sequence[float] | None = None) -> tuple[float, ...]:
    """Per-channel weights.  ``layout`` is a name in LAYOUTS, a comma list such as ``"1,1,1,0,1.41,1.41"``,
    a sequence of numbers, or None to pick mono, stereo or 5.1 from the channel count."""
    if layout is None:
        names = {1: "mono", 2: "stereo", 6: "5.1"}
        if channels not in names:
            raise LoudnessError(f"{channels} channels have no default layout; pass a layout name or weights")
        layout = names[channels]
    if isinstance(layout, str):
        if layout in LAYOUTS:
            weights = LAYOUTS[layout]
        else:
            try:
                weights = tuple(float(part) for part in layout.split(","))
            except ValueError as error:
                raise LoudnessError(f"unknown layout {layout!r}; use {sorted(LAYOUTS)} or a comma list of weights") from error
    else:
        weights = tuple(float(w) for w in layout)
    if len(weights) != channels:
        raise LoudnessError(f"layout has {len(weights)} weights for {channels} channels")
    if any(w < 0 or not math.isfinite(w) for w in weights):
        raise LoudnessError("channel weights must be finite and non-negative")
    return weights


def k_weighting_coefficients(sample_rate: float) -> tuple[tuple[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]:
    """((b, a) pre-filter, (b, a) RLB high-pass) for a sample rate, from the analogue prototype."""
    if sample_rate <= 0:
        raise LoudnessError("sample rate must be positive")
    fs = float(sample_rate)
    f0, gain_db, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = math.tan(math.pi * f0 / fs)
    vh = 10.0 ** (gain_db / 20.0)
    vb = vh ** 0.4996667741545416
    a0 = 1.0 + k / q + k * k
    pre = (np.array([(vh + vb * k / q + k * k) / a0, 2.0 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0]),
           np.array([1.0, 2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0]))
    f0, q = 38.13547087602444, 0.5003270373238773
    k = math.tan(math.pi * f0 / fs)
    a0 = 1.0 + k / q + k * k
    rlb = (np.array([1.0, -2.0, 1.0]), np.array([1.0, 2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0]))
    return pre, rlb


def k_weight(audio: np.ndarray, sample_rate: float) -> np.ndarray:
    (b1, a1), (b2, a2) = k_weighting_coefficients(sample_rate)
    return lfilter(b2, a2, lfilter(b1, a1, _as_2d(audio), axis=0), axis=0)


def _block_power(weighted: np.ndarray, weights: tuple[float, ...], sample_rate: float, block_s: float, hop_s: float) -> np.ndarray:
    """sum over channels of G_i * mean-square, for each full block (a trailing partial block is not measured)."""
    size, hop = int(round(block_s * sample_rate)), int(round(hop_s * sample_rate))
    if len(weighted) < size:
        return np.zeros(0)
    energy = np.vstack([np.zeros((1, weighted.shape[1])), np.cumsum(np.square(weighted), axis=0)])
    starts = np.arange(0, len(weighted) - size + 1, hop)
    mean_square = (energy[starts + size] - energy[starts]) / size
    return mean_square @ np.asarray(weights)


def _loudness(power: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore"):
        return OFFSET + 10.0 * np.log10(power)


def _gated_mean(power: np.ndarray, relative_gate: float) -> tuple[float | None, np.ndarray]:
    """Gated mean loudness and the mask of blocks kept, per BS.1770-4: absolute gate, then a relative gate."""
    loud = _loudness(power)
    absolute = loud > ABSOLUTE_GATE
    if not absolute.any():
        return None, absolute
    threshold = OFFSET + 10.0 * math.log10(power[absolute].mean()) + relative_gate
    keep = absolute & (loud > threshold)
    return OFFSET + 10.0 * math.log10(power[keep].mean()), keep


def true_peak(audio: np.ndarray, sample_rate: float) -> tuple[float, float]:
    """(sample peak, true peak) as linear amplitudes, oversampled per BS.1770-4 Annex 2."""
    audio = _as_2d(audio)
    sample_peak = float(np.abs(audio).max()) if audio.size else 0.0
    factor = 4 if sample_rate < 96000 else 2 if sample_rate < 192000 else 1
    if factor == 1 or not audio.size:
        return sample_peak, sample_peak
    over = resample_poly(audio, factor, 1, axis=0)
    return sample_peak, max(sample_peak, float(np.abs(over).max()))


def measure(audio: np.ndarray, sample_rate: float, layout: str | Sequence[float] | None = None) -> dict[str, Any]:
    """Integrated loudness, loudness range, maxima, sample and true peak.  Raises on audio under 400 ms."""
    audio = _as_2d(audio)
    weights = channel_weights(audio.shape[1], layout)
    if len(audio) < int(round(BLOCK_S * sample_rate)):
        raise LoudnessError(f"audio is shorter than one {int(BLOCK_S * 1000)} ms measurement block")
    weighted = k_weight(audio, sample_rate)
    momentary = _block_power(weighted, weights, sample_rate, BLOCK_S, HOP_S)
    integrated, keep = _gated_mean(momentary, RELATIVE_GATE)
    short = _block_power(weighted, weights, sample_rate, SHORT_TERM_S, HOP_S)
    lra = lra_low = lra_high = None
    if len(short):
        mean, keep_short = _gated_mean(short, LRA_RELATIVE_GATE)
        if mean is not None:
            lra_low, lra_high = (float(v) for v in np.percentile(_loudness(short)[keep_short], [10, 95]))
            lra = lra_high - lra_low
    sample_peak, tp = true_peak(audio, sample_rate)
    r = lambda v, n=3: None if v is None or not math.isfinite(float(v)) else round(float(v), n)  # noqa: E731
    return {
        "schema": SCHEMA,
        "sample_rate": int(sample_rate),
        "channels": int(audio.shape[1]),
        "channel_weights": list(weights),
        "seconds": round(len(audio) / sample_rate, 4),
        "integrated_lufs": r(integrated),
        "gated_blocks": int(keep.sum()),
        "total_blocks": int(len(momentary)),
        "loudness_range_lu": r(lra),
        "loudness_range_low_lufs": r(lra_low),
        "loudness_range_high_lufs": r(lra_high),
        "momentary_max_lufs": r(_loudness(momentary).max()),
        "short_term_max_lufs": r(_loudness(short).max()) if len(short) else None,
        "sample_peak_dbfs": r(db(sample_peak)),
        "true_peak_dbtp": r(db(tp)),
        "audio_sha256": audio_sha256(audio),
    }


def apply_gain(audio: np.ndarray, gain_db: float) -> np.ndarray:
    return np.asarray(audio, dtype=np.float64) * 10.0 ** (gain_db / 20.0)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalize_spec(spec: str | Mapping[str, Any]) -> dict[str, Any]:
    """A preset name or a spec mapping, validated: ``integrated_lufs`` {target, tolerance},
    optional ``true_peak_dbtp_max`` and ``loudness_range_max_lu``."""
    if isinstance(spec, str):
        if spec not in PRESETS:
            raise LoudnessError(f"unknown preset {spec!r}; use {sorted(PRESETS)} or a spec file")
        return dict(PRESETS[spec])
    out = dict(spec)
    target = out.get("integrated_lufs")
    if not isinstance(target, Mapping) or not _number(target.get("target")):
        raise LoudnessError("spec needs integrated_lufs: {target: finite number, tolerance: finite number}")
    tolerance = target.get("tolerance", 0.5)
    if not _number(tolerance) or tolerance < 0:
        raise LoudnessError("integrated_lufs.tolerance must be a finite, non-negative number")
    out["integrated_lufs"] = {"target": float(target["target"]), "tolerance": float(tolerance)}
    for key in ("true_peak_dbtp_max", "loudness_range_max_lu"):
        if key in out and not _number(out[key]):
            raise LoudnessError(f"{key} must be a finite number")
    out.setdefault("name", "custom")
    return out


def check(measurement: Mapping[str, Any], spec: str | Mapping[str, Any]) -> list[str]:
    """Every way a measurement falls outside a delivery spec; an empty list means it complies."""
    spec = normalize_spec(spec)
    problems: list[str] = []
    integrated = measurement["integrated_lufs"]
    target, tolerance = spec["integrated_lufs"]["target"], spec["integrated_lufs"]["tolerance"]
    if integrated is None:
        problems.append("integrated loudness is undefined: no block is above the -70 LUFS gate")
    elif abs(integrated - target) > tolerance + 1e-9:
        problems.append(f"integrated loudness {integrated} LUFS is {integrated - target:+.2f} LU from the {target} LUFS target (tolerance {tolerance} LU)")
    ceiling = spec.get("true_peak_dbtp_max")
    if ceiling is not None and measurement["true_peak_dbtp"] > ceiling + 1e-9:
        problems.append(f"true peak {measurement['true_peak_dbtp']} dBTP is over the {ceiling} dBTP ceiling")
    lra_max = spec.get("loudness_range_max_lu")
    if lra_max is not None:
        if measurement["loudness_range_lu"] is None:
            problems.append("loudness range is undefined (under 3 s of gated programme)")
        elif measurement["loudness_range_lu"] > lra_max + 1e-9:
            problems.append(f"loudness range {measurement['loudness_range_lu']} LU is over the {lra_max} LU limit")
    return problems


def plan_gain(measurement: Mapping[str, Any], target_lufs: float, ceiling_dbtp: float | None, on_ceiling: str = "refuse") -> dict[str, Any]:
    """One linear gain to reach ``target_lufs``.  If it would put the true peak over ``ceiling_dbtp``:
    ``refuse`` returns ``ok`` False with the reason; ``cap`` lowers the gain to meet the ceiling and says
    how far short of the target the result stays."""
    if on_ceiling not in ("refuse", "cap"):
        raise LoudnessError("on_ceiling must be 'refuse' or 'cap'")
    if not _number(target_lufs) or (ceiling_dbtp is not None and not _number(ceiling_dbtp)):
        raise LoudnessError("target loudness and ceiling must be finite numbers")
    integrated = measurement["integrated_lufs"]
    if integrated is None:
        return {"ok": False, "reason": "integrated loudness is undefined (silent or under the -70 LUFS gate)", "gain_db": 0.0}
    wanted = target_lufs - integrated
    plan: dict[str, Any] = {"target_lufs": target_lufs, "ceiling_dbtp": ceiling_dbtp, "wanted_gain_db": round(wanted, 4), "on_ceiling": on_ceiling}
    predicted_peak = measurement["true_peak_dbtp"] + wanted
    if ceiling_dbtp is not None and predicted_peak > ceiling_dbtp + 1e-9:
        if on_ceiling == "refuse":
            plan.update(ok=False, gain_db=0.0, reason=f"gain {wanted:+.2f} dB would put the true peak at {predicted_peak:.2f} dBTP, over the {ceiling_dbtp} dBTP ceiling")
            return plan
        gain = ceiling_dbtp - measurement["true_peak_dbtp"]
        plan.update(ok=True, gain_db=round(gain, 4), capped=True, shortfall_lu=round(target_lufs - (integrated + gain), 4))
        return plan
    plan.update(ok=True, gain_db=round(wanted, 4), capped=False, shortfall_lu=0.0)
    return plan


def solve_gain(audio: np.ndarray, sample_rate: float, target_lufs: float, ceiling_dbtp: float | None,
               on_ceiling: str = "refuse", layout: str | Sequence[float] | None = None, tolerance: float = 0.01,
               max_iterations: int = 16) -> dict[str, Any]:
    """``plan_gain`` made exact for the audio itself.  A gain moves every block by the same amount, but it
    can also move blocks across the -70 LUFS absolute gate or the 10 LU relative gate, so the integrated
    loudness after the gain is not always the loudness before plus the gain.  This re-measures the scaled
    audio and corrects the gain until the measured result is within ``tolerance`` LU of the target; if the
    gates make the target unreachable with one gain it says so instead of reporting success."""
    before = measure(audio, sample_rate, layout)
    plan = plan_gain(before, target_lufs, ceiling_dbtp, on_ceiling)
    if not plan["ok"]:
        return plan
    gain = plan["gain_db"]
    plan["first_gain_db"] = gain
    reached = None
    for iteration in range(1, max_iterations + 1):
        reached = measure(apply_gain(audio, gain), sample_rate, layout)["integrated_lufs"]
        plan["iterations"] = iteration
        plan["predicted_lufs"] = reached
        if reached is None:
            break
        error = target_lufs - reached
        if abs(error) <= tolerance or plan.get("capped"):
            break
        gain += error
        if ceiling_dbtp is not None and before["true_peak_dbtp"] + gain > ceiling_dbtp + 1e-9:
            if on_ceiling == "refuse":
                plan.update(ok=False, gain_db=0.0, reason=f"reaching {target_lufs} LUFS needs {gain:+.2f} dB once the gates are accounted for, "
                            f"which would put the true peak at {before['true_peak_dbtp'] + gain:.2f} dBTP, over the {ceiling_dbtp} dBTP ceiling")
                return plan
            gain = ceiling_dbtp - before["true_peak_dbtp"]
            plan["capped"] = True
    plan["gain_db"] = round(gain, 4)
    if reached is None or (not plan.get("capped") and abs(target_lufs - reached) > tolerance):
        plan.update(ok=False, reason=f"no single gain reaches {target_lufs} LUFS: the nearest measured result is {reached} LUFS "
                    "(blocks cross the -70 LUFS or relative gate as the level changes)")
    elif plan.get("capped"):
        plan["shortfall_lu"] = round(target_lufs - reached, 4)
    return plan


def synthetic_programme(sample_rate: int = 48000, seed: int = 0) -> np.ndarray:
    """Invented stereo programme: 16 s of harmonic bursts over a quiet room tone, a loud passage in the
    middle.  Stands in for dialogue; no recording is involved."""
    rng = np.random.default_rng(seed)
    n = 16 * sample_rate
    t = np.arange(n) / sample_rate
    room = 0.002 * rng.standard_normal((n, 2))
    voice = np.zeros(n)
    for f0 in (110.0, 147.0, 196.0):
        voice += sum(np.sin(2 * math.pi * f0 * h * t) / h for h in range(1, 9))
    envelope = np.clip(np.sin(2 * math.pi * 0.9 * t), 0, None) ** 2
    level = np.where((t > 6) & (t < 11), 0.5, 0.12)
    mono = voice * envelope * level / 4.0
    pan = np.array([0.9, 0.75])
    return room + mono[:, None] * pan[None, :]
