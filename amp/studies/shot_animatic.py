"""Deterministic animatic for a validated shot plan: one plan-view card per shot, a contact sheet,
a timeline for assembly and an optional ffmpeg video.

Each card shows the set bounds, elements, performers and the shot camera with its horizontal field
of view, plus the shot ID, lens, frame ranges and action.  Cards are pure functions of the plan, so
identical plans give identical PNG bytes and hashes.  The video holds each card for the shot duration.
"""
from __future__ import annotations

import hashlib
import io
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from PIL import Image, ImageDraw, ImageFont

from . import shot_plan as sp

CARD = (640, 360)
PLAN_BOX = (16, 16, 336, 344)
BACKGROUND, GRID, SET_COLOUR = (24, 26, 30), (50, 54, 62), (96, 104, 120)
ELEMENT, ADDED, PERFORMER, CAMERA, CONE = (200, 170, 90), (230, 110, 90), (110, 190, 240), (120, 230, 150), (60, 120, 80)
TEXT = (230, 232, 236)


def _mapper(bounds):
    x0, x1 = bounds["x"]
    z0, z1 = bounds["z"]
    left, top, right, bottom = PLAN_BOX
    scale = min((right - left) / (x1 - x0), (bottom - top) / (z1 - z0))
    ox = left + ((right - left) - scale * (x1 - x0)) / 2
    oy = top + ((bottom - top) - scale * (z1 - z0)) / 2

    def to_px(x, z):
        return ox + (x - x0) * scale, oy + (z1 - z) * scale  # +z is up the card
    return to_px, scale


def render_card(plan: Mapping[str, Any], shot: Mapping[str, Any], ledger_row: Mapping[str, Any]) -> Image.Image:
    image = Image.new("RGB", CARD, BACKGROUND)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    bounds = plan["set"]["bounds"]
    to_px, scale = _mapper(bounds)
    x0, x1 = bounds["x"]
    z0, z1 = bounds["z"]
    draw.rectangle([*to_px(x0, z1), *to_px(x1, z0)], outline=SET_COLOUR, width=2)
    metre = math.ceil(x0)
    while metre <= x1:
        a, b = to_px(metre, z1), to_px(metre, z0)
        draw.line([a, b], fill=GRID)
        metre += 1
    metre = math.ceil(z0)
    while metre <= z1:
        draw.line([to_px(x0, metre), to_px(x1, metre)], fill=GRID)
        metre += 1
    for element in plan["set"].get("elements", []):
        cx, cz = element["center"]
        radius = element.get("radius", 0.3) * scale
        px, py = to_px(cx, cz)
        colour = ADDED if element.get("added") else ELEMENT
        draw.ellipse([px - radius, py - radius, px + radius, py + radius], outline=colour, width=2)
        draw.text((px + 3, py - 5), element["id"], fill=colour, font=font)
    camera = shot["camera"]
    cx, _, cz = camera["position"]
    ax, _, az = camera["aim"]
    heading = math.atan2(ax - cx, az - cz)
    half = math.radians(sp.hfov_deg(camera["lens_mm"], camera.get("sensor_width_mm", 36.0))) / 2
    reach = 0.6 * max(x1 - x0, z1 - z0)
    origin = to_px(cx, cz)
    left_tip = to_px(cx + reach * math.sin(heading - half), cz + reach * math.cos(heading - half))
    right_tip = to_px(cx + reach * math.sin(heading + half), cz + reach * math.cos(heading + half))
    draw.polygon([origin, left_tip, right_tip], fill=CONE)
    draw.line([origin, left_tip], fill=CAMERA)
    draw.line([origin, right_tip], fill=CAMERA)
    draw.ellipse([origin[0] - 5, origin[1] - 5, origin[0] + 5, origin[1] + 5], fill=CAMERA)
    for performer in plan.get("performers", []):
        px, py = to_px(*performer["position"])
        listed = performer["id"] in shot.get("performers", [])
        draw.ellipse([px - 6, py - 6, px + 6, py + 6], fill=PERFORMER if listed else None, outline=PERFORMER, width=2)
        draw.text((px + 8, py - 5), performer["id"], fill=PERFORMER, font=font)
    lines = [f"{shot['id']}  {camera['name']}", f"{camera['lens_mm']} mm  hfov {ledger_row['hfov_deg']:.1f} deg",
             f"scene frames {ledger_row['scene_frames'][0]}-{ledger_row['scene_frames'][1]}",
             f"render {ledger_row['render_frames'][0]}-{ledger_row['render_frames'][1]} ({ledger_row['render_frame_count']} frames)",
             f"edit {ledger_row['edit_in']}-{ledger_row['edit_out']}  {ledger_row['duration_s']} s",
             f"receipt: {ledger_row['receipt'].get('status', 'planned')}", ""]
    lines += _wrap(shot.get("action", ""), 38)
    for number, line in enumerate(lines):
        draw.text((356, 20 + number * 14), line, fill=TEXT, font=font)
    return image


def _wrap(text: str, width: int) -> list[str]:
    words, rows, row = text.split(), [], ""
    for word in words:
        if len(row) + len(word) + 1 > width and row:
            rows.append(row)
            row = word
        else:
            row = f"{row} {word}".strip()
    return rows + ([row] if row else [])


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=False, compress_level=6)
    return buffer.getvalue()


def write_animatic(plan: Mapping[str, Any], output: Path, video: bool = False) -> dict[str, Any]:
    ledger = sp.build_ledger(plan)
    output = Path(output).resolve()
    cards = output / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    images, hashes = [], {}
    for shot, row in zip(plan["shots"], ledger["shots"]):
        image = render_card(plan, shot, row)
        data = _png_bytes(image)
        (cards / f"{shot['id']}.png").write_bytes(data)
        hashes[shot["id"]] = hashlib.sha256(data).hexdigest()
        images.append(image)
    columns = min(3, len(images))
    rows = math.ceil(len(images) / columns)
    sheet = Image.new("RGB", (CARD[0] * columns, CARD[1] * rows), BACKGROUND)
    for index, image in enumerate(images):
        sheet.paste(image, ((index % columns) * CARD[0], (index // columns) * CARD[1]))
    sheet_bytes = _png_bytes(sheet)
    (output / "contact-sheet.png").write_bytes(sheet_bytes)
    (output / "shot-ledger.json").write_text(sp.ledger_json(ledger) + "\n")
    (output / "edit.edl").write_text(sp.build_edl(plan, ledger))
    result = {"shots": len(images), "total_seconds": ledger["total_seconds"], "plan_sha256": ledger["plan_sha256"],
              "card_sha256": hashes, "contact_sheet_sha256": hashlib.sha256(sheet_bytes).hexdigest(), "video": None}
    if video:
        result["video"] = _write_video(plan, ledger, output)
    return result


def _write_video(plan: Mapping[str, Any], ledger: Mapping[str, Any], output: Path) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required for --video and was not found on PATH")
    fps = plan["fps"]
    concat = output / "animatic.ffconcat"
    rows = ["ffconcat version 1.0"]
    for row in ledger["shots"]:
        rows += [f"file cards/{row['id']}.png", f"duration {row['duration_s']}"]
    rows.append(f"file cards/{ledger['shots'][-1]['id']}.png")  # concat demuxer drops the last duration without a repeat
    concat.write_text("\n".join(rows) + "\n")
    target = output / "animatic.mp4"
    command = [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
               "-vf", f"fps={fps},format=yuv420p", "-frames:v", str(ledger["total_frames"]),
               "-c:v", "libx264", "-preset", "veryfast", "-threads", "2", str(target)]
    subprocess.run(command, check=True, cwd=output)
    return {"path": target.name, "frames": ledger["total_frames"], "fps": fps}
