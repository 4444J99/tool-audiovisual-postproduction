"""One-command synthetic short-film demo: the whole sealed chain, then a small playable mp4 checked against it.

    python tools/short_film_demo.py run    --output outputs/short-film-demo [--delivery ebu_r128|atsc_a85|podcast|spec.json] [--qc-spec spec.json]
    python tools/short_film_demo.py verify --output outputs/short-film-demo

``run`` executes ``tools/av_pipeline.py``'s chain (set spec, shot plan, coverage, room-tone fill, loudness delivery, colour plan,
captions, caption burn-in, delivery QC), verifies it, and muxes the burned-in frames and the delivered dialogue into
``short-film.mp4`` (H.264, lossless luma, AAC, 24 fps, 240x48).  It then writes ``demo-hashes.json`` (SHA-256 of every artefact
and of the mp4) and ``demo-qc.json`` (the mp4 judged against the chain).  ``verify`` re-checks the chain, the hash file and the mp4.
Every input is invented.  Nothing is played: ffmpeg only encodes and decodes to files, and no audio device or browser is opened.
Exit status 2: any chain, ffmpeg, hash, mp4 or output-directory problem; the problems print as JSON.  ``run`` refuses an unsuitable ``--output`` before deleting anything
and replaces a previous output only by a complete new one (docs/DEMO.md).
"""
import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from amp.studies import loudness as ld  # noqa: E402
from amp.studies import pipeline_chain as pc  # noqa: E402

MP4 = "short-film.mp4"
HASHES = "demo-hashes.json"
DEMO_QC = "demo-qc.json"
SCHEMA = "amps.short_film_demo.v1"
MARKER = ".short-film-demo"  # written first, so a failed run can still be replaced; its text is the schema name
LOUDNESS_TOLERANCE_LU = 1.0  # AAC is lossy: the mp4's audio must stay within this of the delivered WAV
DURATION_TOLERANCE_S = 0.05
AUDIO_MIN_SNR_DB = 15.0  # the mp4's sound against the delivered dialogue, after AAC; a re-encode measures about 33 dB, noise at the same loudness about -3 dB
AUDIO_MAX_LAG = 512  # samples (32 ms) the decoded audio may sit from the delivered WAV; the edit list trims AAC priming, so an honest decode measures exactly 0
PADDING_GUARD = 1024  # samples after the dialogue that AAC may still ring into (measured: at most 0.0003 inside it, exactly 0 after it)
PADDING_PEAK = 0.001  # after the guard the padding must stay below this (-60 dBFS); the clean demo is exactly zero
GUARD_PEAK = 0.003  # inside the guard, ten times the measured ringing, so a click there is still named
BLOCK_SAMPLES = 1600  # 100 ms blocks for the blockwise SNR
BLOCK_FLOOR_RMS = 0.003  # blocks of the delivered dialogue quieter than this (-50 dBFS) are not judged blockwise
AUDIO_MIN_BLOCK_SNR_DB = 6.0  # every judged block; an honest encode measures at least 12.9 (the first block) and typically 22 or more
CHROMA_TOLERANCE = 1  # the chain writes flat 128 chroma, and the video is lossless


class DemoError(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(errors))
        self.errors = errors


def sha256_file(path):
    return pc.sha256_file(Path(path))


def ffmpeg_tools():
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise DemoError(["ffmpeg and ffprobe are required to build the mp4"])
    encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    missing = [name for name in ("libx264", " aac ") if name not in encoders]
    if missing:
        raise DemoError([f"this ffmpeg lacks the encoder {name.strip()}" for name in missing])
    return ffmpeg, ffprobe


def ffmpeg_version(ffmpeg):
    return subprocess.run([ffmpeg, "-version"], capture_output=True, text=True).stdout.splitlines()[0]


def yuv420_stream(frames, width, height):
    """The luma frames with two flat 128 chroma planes per frame: a yuv420p stream whose Y plane is the frames, byte for byte."""
    size = width * height
    chroma = bytes([128]) * (2 * (width // 2) * (height // 2))
    parts = []
    for offset in range(0, len(frames), size):
        parts.append(frames[offset:offset + size])
        parts.append(chroma)
    return b"".join(parts)


def mux(out, ledger, burnin, ffmpeg):
    """Encode frames + delivered dialogue into one mp4 with settings that make the bytes repeatable."""
    params = burnin["params"]["geometry"]
    width, height = params["width"], params["height"]
    fps = ledger["fps"]
    frames = (out / "captions" / "frames.y8").read_bytes()
    if len(frames) != width * height * ledger["total_frames"]:
        raise DemoError([f"frames.y8 holds {len(frames)} bytes; the ledger's {ledger['total_frames']} frames of {width}x{height} need {width * height * ledger['total_frames']}"])
    target = out / MP4
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{width}x{height}", "-r", str(fps), "-i", "pipe:0",
               "-i", str(out / "audio" / "dialogue-delivered.wav"),
               "-map", "0:v:0", "-map", "1:a:0",
               "-af", f"apad=whole_dur={ledger['total_seconds']}",
               "-c:v", "libx264", "-qp", "0", "-preset", "veryslow", "-pix_fmt", "yuv420p", "-threads", "1",
               "-c:a", "aac", "-b:a", "64k", "-ar", "16000", "-ac", "1",
               "-t", str(ledger["total_seconds"]),
               "-map_metadata", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
               "-movflags", "+faststart", str(target)]
    run = subprocess.run(command, input=yuv420_stream(frames, width, height), capture_output=True)
    if run.returncode != 0:
        raise DemoError([f"ffmpeg could not write the mp4: {run.stderr.decode(errors='replace')[-600:]}"])
    return target


def probe(ffprobe, path):
    run = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], capture_output=True, text=True)
    if run.returncode != 0:
        raise DemoError([f"ffprobe could not read {path.name}: {run.stderr[-300:]}"])
    return json.loads(run.stdout)


def decode(ffmpeg, path, *args):
    run = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path), *args, "-f", "rawvideo" if "-pix_fmt" in args else "s16le", "pipe:1"], capture_output=True)
    if run.returncode != 0:
        raise DemoError([f"ffmpeg could not decode {path.name}: {run.stderr.decode(errors='replace')[-300:]}"])
    return run.stdout


def align(heard, reference):
    """``heard`` shifted to the best lag within AUDIO_MAX_LAG samples of ``reference`` (cross-correlation by FFT), padded or cut to its length; None if too short."""
    n = len(reference)
    if len(heard) < n or not np.any(reference):
        return None
    size = 1 << (n + 2 * AUDIO_MAX_LAG).bit_length()  # room for both signs of lag: the correlation is circular
    window = heard[:n + AUDIO_MAX_LAG]
    cross = np.fft.irfft(np.fft.rfft(window, size) * np.conj(np.fft.rfft(reference, size)), size)
    lags = np.concatenate([np.arange(0, AUDIO_MAX_LAG + 1), np.arange(-AUDIO_MAX_LAG, 0)])
    lag = int(lags[np.argmax(np.concatenate([cross[:AUDIO_MAX_LAG + 1], cross[size - AUDIO_MAX_LAG:]]))])
    shifted = heard[lag:lag + n] if lag >= 0 else np.concatenate([np.zeros(min(-lag, n)), heard[:max(n + lag, 0)]])
    if len(shifted) < n:  # the track ends before the dialogue does: the missing samples count as silence, so the SNR drops
        shifted = np.concatenate([shifted, np.zeros(n - len(shifted))])
    return shifted[:n]


def snr_db(shifted, reference):
    error = float(np.sum((shifted - reference) ** 2))
    return float("inf") if error == 0 else float(10 * np.log10(np.sum(reference ** 2) / error))


def aligned_snr_db(heard, reference):
    """SNR of ``heard`` against ``reference`` at the best lag within AUDIO_MAX_LAG samples."""
    shifted = align(heard, reference)
    return float("-inf") if shifted is None else snr_db(shifted, reference)


def worst_block_snr_db(heard, reference):
    """The lowest SNR over the 100 ms blocks of ``reference`` that are louder than BLOCK_FLOOR_RMS, and the start (sample) of that block.

    A trailing block shorter than 100 ms is judged like the others, so a muted or replaced end of the dialogue cannot hide in it."""
    shifted = align(heard, reference)
    if shifted is None:
        return float("-inf"), 0
    worst, at = float("inf"), 0
    for start in range(0, len(reference), BLOCK_SAMPLES):
        block = reference[start:start + BLOCK_SAMPLES]
        if np.sqrt(np.mean(block ** 2)) < BLOCK_FLOOR_RMS:
            continue
        value = snr_db(shifted[start:start + BLOCK_SAMPLES], block)
        if value < worst:
            worst, at = value, start
    return worst, at


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def judge_mp4(out, ledger, burnin, tools):
    """Checks that tie the mp4 back to the sealed chain.  Each entry names what it compared."""
    ffmpeg, ffprobe = tools
    path = out / MP4
    info = probe(ffprobe, path)
    video = [s for s in info["streams"] if s["codec_type"] == "video"]
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    width, height = burnin["params"]["geometry"]["width"], burnin["params"]["geometry"]["height"]
    checks = []

    def add(check_id, ok, detail, **facts):
        checks.append({"id": check_id, "ok": bool(ok), "detail": detail, **facts})

    add("mp4.container", "mp4" in str(info.get("format", {}).get("format_name", "")).split(","), "the file is an MP4 container (a remux into another container is refused)", observed=info.get("format", {}).get("format_name"))
    add("mp4.streams", len(video) == 1 and len(audio) == 1 and len(info["streams"]) == 2, "one video and one audio stream", streams=[s["codec_name"] for s in info["streams"]])
    if len(video) != 1 or len(audio) != 1:
        return checks
    v, a = video[0], audio[0]
    seen = [v.get("codec_name"), v.get("width"), v.get("height"), v.get("avg_frame_rate")]
    add("mp4.video_geometry", seen == ["h264", width, height, f"{ledger['fps']}/1"],
        "H.264 at the burn-in geometry and the ledger's exact frame rate", observed=seen, expected=["h264", width, height, f"{ledger['fps']}/1"])
    pixel_format, color_range, aspect = v.get("pix_fmt"), v.get("color_range"), v.get("sample_aspect_ratio")
    colour = [v.get("color_space"), v.get("color_transfer"), v.get("color_primaries")]
    add("mp4.video_pixel_format", pixel_format == "yuv420p" and color_range in (None, "tv", "unknown") and aspect in (None, "1:1", "0:1", "N/A")
        and all(flag in (None, "unknown") for flag in colour),
        "yuv420p, limited or unflagged range, square pixels, no colour matrix, transfer or primaries flags: the chroma check reads the stream as it is, so a 4:4:4, full-range or re-flagged stream is refused",
        observed=[pixel_format, color_range, aspect, *colour])
    heard_format = [a.get("codec_name"), as_int(a.get("sample_rate")), a.get("channels")]
    add("mp4.audio_format", heard_format == ["aac", 16000, 1], "AAC, 16 kHz, mono", observed=heard_format)
    duration = as_float(info.get("format", {}).get("duration"))
    add("mp4.duration", duration is not None and abs(duration - ledger["total_seconds"]) <= DURATION_TOLERANCE_S, "container duration equals the final ledger's total seconds",
        observed_s=None if duration is None else round(duration, 3), ledger_s=ledger["total_seconds"])
    frames = as_int(v.get("nb_frames"))
    add("mp4.frame_count", frames == ledger["total_frames"], "the video holds exactly the ledger's frames", observed=frames, ledger=ledger["total_frames"])

    decoded = decode(ffmpeg, path, "-map", "0:v:0", "-pix_fmt", "yuv420p")
    size = width * height
    plane = width * height + 2 * (width // 2) * (height // 2)
    count = len(decoded) // plane
    luma = b"".join(decoded[i * plane:i * plane + size] for i in range(count))
    luma_hash = hashlib.sha256(luma).hexdigest()
    add("mp4.luma_equals_burnin", luma_hash == burnin["frames_sha256"], "the decoded luma plane equals captions/frames.y8, byte for byte (lossless video)",
        decoded_luma_sha256=luma_hash, frames_sha256=burnin["frames_sha256"])
    planes = np.frombuffer(decoded[:count * plane], dtype=np.uint8).reshape(count, plane)[:, size:] if count else np.zeros((0, 0), dtype=np.uint8)
    deviation = int(np.abs(planes.astype(np.int16) - 128).max()) if planes.size else 255
    add("mp4.chroma_flat", deviation <= CHROMA_TOLERANCE, f"the chroma planes are flat grey (the chain writes 128; video is lossless), within {CHROMA_TOLERANCE}", max_deviation=deviation)

    pcm = np.frombuffer(decode(ffmpeg, path, "-map", "0:a:0", "-ar", "16000", "-ac", "1"), dtype="<i2").astype(np.float64) / 32768.0
    rate, delivered = pc.read_pcm16(out / "audio" / "dialogue-delivered.wav")
    try:
        reference = ld.measure(delivered, rate)["integrated_lufs"]
        heard = ld.measure(pcm[:len(delivered)], rate)["integrated_lufs"]
        add("mp4.audio_loudness", abs(heard - reference) <= LOUDNESS_TOLERANCE_LU, f"the mp4's audio measures within {LOUDNESS_TOLERANCE_LU} LU of the delivered dialogue (AAC is lossy)",
            delivered_lufs=round(reference, 2), mp4_lufs=round(heard, 2))
    except ld.LoudnessError as error:
        add("mp4.audio_loudness", False, f"the mp4's audio cannot be measured: {error}")
    snr = aligned_snr_db(pcm, delivered)
    add("mp4.audio_matches_delivered", snr >= AUDIO_MIN_SNR_DB, f"the mp4's sound is the delivered dialogue: at least {AUDIO_MIN_SNR_DB} dB SNR against dialogue-delivered.wav (AAC is lossy)",
        snr_db=None if snr in (float("inf"), float("-inf")) else round(snr, 1))
    block, at = worst_block_snr_db(pcm, delivered)
    add("mp4.audio_blocks_match_delivered", block >= AUDIO_MIN_BLOCK_SNR_DB,
        f"every 100 ms block of the dialogue louder than -50 dBFS is there at least {AUDIO_MIN_BLOCK_SNR_DB} dB SNR: a muted or replaced stretch cannot hide in a whole-track figure",
        worst_block_snr_db=None if block in (float("inf"), float("-inf")) else round(block, 1), worst_block_at_s=round(at / rate, 2))
    inside = pcm[len(delivered):len(delivered) + PADDING_GUARD]
    guard_peak = float(np.abs(inside).max()) if inside.size else 0.0
    padding = pcm[len(delivered) + PADDING_GUARD:]
    peak = float(np.abs(padding).max()) if padding.size else 0.0
    add("mp4.audio_padding_silent", guard_peak <= GUARD_PEAK and peak <= PADDING_PEAK,
        f"after the dialogue the mp4's padding is silent: below {GUARD_PEAK} for the first {PADDING_GUARD} samples (AAC rings there), below {PADDING_PEAK} after", guard_peak=round(guard_peak, 4), padding_peak=round(peak, 4))
    return checks


def artefact_hashes(out):
    """SHA-256 of every file the run wrote, except the demo's own bookkeeping files at the top of the directory."""
    own = {HASHES, DEMO_QC, MARKER}
    return {str(p.relative_to(out)): sha256_file(p) for p in sorted(out.rglob("*")) if p.is_file() and str(p.relative_to(out)) not in own}


ENCODER_NOTE = "the mp4 bytes depend on this ffmpeg/libx264/aac build; they repeat on one build"


def chain_fields(manifest):
    """The fields of demo-hashes.json that the sealed chain determines, so verify can recompute and compare them."""
    stages = {entry["stage"]: entry for entry in manifest["stages"]}
    return {
        "schema": SCHEMA,
        "chain_sha256": manifest["chain_sha256"],
        "stages": [entry["stage"] for entry in manifest["stages"]],
        "deterministic": {
            "frames_sha256": stages["burnin"]["outputs"]["burnin-frames"],
            "delivered_dialogue_sha256": stages["delivery"]["outputs"]["dialogue-delivered"],
            "qc_report_sha256": stages["qc"]["outputs"]["qc-report"],
            "captions_srt_sha256": stages["captions"]["outputs"]["captions-srt"],
        },
    }


def collect_hashes(out, manifest, ffmpeg_line):
    files = artefact_hashes(out)
    return {
        **chain_fields(manifest),
        "encoder": {"ffmpeg": ffmpeg_line, "note": ENCODER_NOTE},
        "mp4_sha256": sha256_file(out / MP4),
        "files": files,
    }


def same_report(recorded, fresh):
    """Equal JSON values, except that a float may differ by a rounding step (the facts are rounded floats measured on this host)."""
    if isinstance(recorded, dict) and isinstance(fresh, dict):
        return recorded.keys() == fresh.keys() and all(same_report(recorded[k], fresh[k]) for k in fresh)
    if isinstance(recorded, list) and isinstance(fresh, list):
        return len(recorded) == len(fresh) and all(same_report(r, f) for r, f in zip(recorded, fresh))
    if isinstance(recorded, float) and isinstance(fresh, float):
        return math.isclose(recorded, fresh, abs_tol=0.05)
    return recorded == fresh and type(recorded) is type(fresh)


def sealed_problems(recorded, manifest):
    """What demo-hashes.json claims that the chain, not the mp4, can check: shape, schema, chain, stages, deterministic hashes, encoder note."""
    if not isinstance(recorded, dict):
        return ["demo-hashes.json is not a JSON object"]
    problems = []
    for key, expected in chain_fields(manifest).items():
        if recorded.get(key) != expected:
            problems.append(f"demo-hashes.json {key} differs from the sealed chain" if key != "chain_sha256" else "demo-hashes.json names a chain hash that differs from pipeline-manifest.json")
    encoder = recorded.get("encoder")
    if not (isinstance(encoder, dict) and isinstance(encoder.get("ffmpeg"), str) and encoder["ffmpeg"].startswith("ffmpeg version") and encoder.get("note") == ENCODER_NOTE):
        problems.append("demo-hashes.json encoder is not an ffmpeg version line and the encoder note")
    files = recorded.get("files")
    if not (isinstance(files, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())):
        problems.append("demo-hashes.json files is not an object of file name to hash")
    if not isinstance(recorded.get("mp4_sha256"), str):
        problems.append("demo-hashes.json mp4_sha256 is not a hash")
    return problems


def inspect_output(out):
    """Everything that can refuse an ``--output``, checked before anything is written or deleted.

    Returns True when ``out`` holds a previous demo output that a run replaces, False when it is absent or empty."""
    if out.is_symlink():
        raise DemoError([f"{out} is a symbolic link; choose a real directory"])
    if out.exists() and not out.is_dir():
        raise DemoError([f"{out} exists and is not a directory; choose a new directory"])
    here, target = Path.cwd().resolve(), out.resolve()
    if target == here or target in here.parents:
        raise DemoError([f"{out} is the working directory or contains it; choose another directory (a run replaces its output, which would remove the directory it runs in)"])
    if not (out.exists() and any(out.iterdir())):
        return False
    marker = out / MARKER
    try:
        marked = marker.is_file() and not marker.is_symlink() and marker.read_text() == SCHEMA
    except (OSError, UnicodeDecodeError):
        marked = False
    if not marked:
        raise DemoError([f"{out} is not empty and is not a previous demo output; choose a new directory"])
    return True


def sibling(out, label):
    """An unused hidden name next to ``out`` (same file system, so renames are atomic)."""
    for number in range(1000):
        candidate = out.parent / f".{out.name}.{label}-{os.getpid()}-{number}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise DemoError([f"no unused name next to {out} for a temporary directory"])


def remove_tree(path):
    """Delete a directory tree, including one whose subdirectories are read-only or unreadable (symbolic links are never followed)."""
    def open_up(directory):
        os.chmod(directory, os.stat(directory).st_mode | 0o700)
        for entry in os.scandir(directory):
            if entry.is_dir(follow_symlinks=False):
                open_up(entry.path)
    open_up(path)
    shutil.rmtree(path)


def replace_directory(staged, out):
    """Put ``staged`` where ``out`` is.  The previous ``out`` stays until the replacement is in place and is put back if the swap fails."""
    backup = sibling(out, "previous")
    out.rename(backup)
    try:
        staged.rename(out)
    except BaseException:
        backup.rename(out)
        raise
    try:
        remove_tree(backup)
    except OSError as error:  # the replacement is in place; a leftover hidden backup is only clutter
        print(f"warning: could not remove the previous output, now {backup}: {error}", file=sys.stderr)


def build_demo(out, tools, delivery, qc_spec):
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text(SCHEMA)
    try:
        manifest = pc.run_chain(out, delivery=delivery or pc.DEFAULT_DELIVERY, qc_spec=qc_spec)
    except pc.ChainError as error:
        raise DemoError(error.errors) from error
    problems = pc.verify_chain(out) + pc.cross_checks(out, delivery, qc_spec)
    if problems:
        raise DemoError(problems)
    ledger = json.loads((out / "plan" / "final-ledger.json").read_text())
    burnin = json.loads((out / "captions" / "burnin-report.json").read_text())
    mux(out, ledger, burnin, tools[0])
    checks = judge_mp4(out, ledger, burnin, tools)
    report = {"schema": SCHEMA, "ok": all(c["ok"] for c in checks), "checks": checks}
    (out / DEMO_QC).write_text(pc.dumps(report))
    if not report["ok"]:
        raise DemoError([f"{c['id']}: {c['detail']}" for c in checks if not c["ok"]])
    hashes = collect_hashes(out, manifest, ffmpeg_version(tools[0]))
    (out / HASHES).write_text(pc.dumps(hashes))
    return hashes


def run_demo(out, delivery=None, qc_spec=None):
    """Write the demo into ``out``.  Every refusal happens before anything is deleted.

    A new or empty ``out`` is written in place, so a failed run leaves its partial files (marker first) to look at and
    can be rerun.  A previous demo output is replaced only by a complete new one: the run is built in a hidden
    sibling directory and swapped in, so a failure at any point leaves the previous output, marker included, as it was."""
    out = Path(out)
    tools = ffmpeg_tools()
    replacing = inspect_output(out)
    if not replacing:
        return build_demo(out, tools, delivery, qc_spec)
    staged = sibling(out, "building")
    try:
        hashes = build_demo(staged, tools, delivery, qc_spec)
        replace_directory(staged, out)
    except BaseException:
        try:
            if staged.exists():
                remove_tree(staged)
        except OSError as error:  # the original failure is the one to report
            print(f"warning: could not remove {staged}: {error}", file=sys.stderr)
        raise
    return hashes


def verify_demo(out, delivery=None, qc_spec=None):
    out = Path(out)
    problems = []
    try:
        recorded = json.loads((out / HASHES).read_text())
        problems += pc.verify_chain(out)
        problems += pc.cross_checks(out, delivery, qc_spec) if not problems else []
    except (OSError, KeyError, json.JSONDecodeError) as error:
        return [f"cannot verify: {error}"]
    if problems:
        return problems
    manifest = json.loads((out / "pipeline-manifest.json").read_text())
    problems = sealed_problems(recorded, manifest)
    if problems:
        return problems
    on_disk = artefact_hashes(out)
    for name in sorted(set(on_disk) | set(recorded["files"])):
        if on_disk.get(name) != recorded["files"].get(name):
            problems.append(f"{name} differs from demo-hashes.json" if name in on_disk and name in recorded["files"] else f"{name} is not in both the directory and demo-hashes.json")
    if on_disk.get(MP4) != recorded["mp4_sha256"]:
        problems.append("mp4_sha256 differs from the mp4 on disk")
    if problems:
        return problems
    try:
        tools = ffmpeg_tools()
        ledger = json.loads((out / "plan" / "final-ledger.json").read_text())
        burnin = json.loads((out / "captions" / "burnin-report.json").read_text())
        checks = judge_mp4(out, ledger, burnin, tools)
    except DemoError as error:
        return error.errors
    failed = [f"{c['id']}: {c['detail']}" for c in checks if not c["ok"]]
    if failed:
        return failed
    try:
        written = json.loads((out / DEMO_QC).read_text())
    except (OSError, ValueError) as error:
        return [f"{DEMO_QC} cannot be read: {error}"]
    if not same_report(written, {"schema": SCHEMA, "ok": True, "checks": checks}):
        return [f"{DEMO_QC} differs from the mp4 judgement verify just made"]
    return []


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("run", "verify"))
    parser.add_argument("--output", default="outputs/short-film-demo")
    parser.add_argument("--delivery", help="loudness preset or spec JSON file (default ebu_r128)")
    parser.add_argument("--qc-spec", help="delivery QC spec JSON file")
    args = parser.parse_args(argv)
    try:
        delivery = None
        if args.delivery:
            sys.path.insert(0, str(ROOT / "tools"))
            import loudness
            delivery = loudness.load_spec(args.delivery)
        qc_spec = json.loads(Path(args.qc_spec).read_text()) if args.qc_spec else None
        pc.qc.normalize_spec(qc_spec)
    except (ValueError, OSError, pc.qc.QcError) as error:
        print(json.dumps({"errors": [f"spec: {error}"]}, indent=2))
        return 2
    try:
        if args.command == "run":
            hashes = run_demo(args.output, delivery, qc_spec)
            print(json.dumps({"chain_sha256": hashes["chain_sha256"], "mp4_sha256": hashes["mp4_sha256"], **hashes["deterministic"],
                              "mp4": str(Path(args.output) / MP4), "hashes": str(Path(args.output) / HASHES), "qc_report": str(Path(args.output) / "qc" / "qc-report.json")}, indent=2, sort_keys=True))
            return 0
        problems = verify_demo(args.output, delivery, qc_spec)
    except DemoError as error:
        print(json.dumps({"errors": error.errors}, indent=2))
        return 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"errors": [f"{args.command}: {type(error).__name__}: {error}"]}, indent=2))
        return 2
    if problems:
        print(json.dumps({"errors": problems}, indent=2))
        return 2
    print(json.dumps({"ok": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
