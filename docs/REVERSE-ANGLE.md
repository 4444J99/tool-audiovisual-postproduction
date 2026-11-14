# Reverse-angle planning and observed-pixel ledger

`amp/studies/reverse_angle.py` and `tools/reverse_angle.py` answer one question about a single-camera recording: what part of the reverse view does the source actually show?

## What it does

1. **Plan.** `plan_reverse_camera` stands the reverse camera behind the second performer on the same side of the axis of action as the source camera, keeps the source eyeline height, and aims it at the first performer with no roll. It refuses (with `ValueError`) a source camera on the axis, coincident performers, non-positive offsets, a camera outside the room, and any placement that crosses the axis. The plan reports the optical-axis separation in degrees and each check it made.
2. **Reproject.** `reproject_observed` casts a ray per reverse-camera pixel into the closed room box built from the `amps.god_here.room_proxy.v1` planes (`depth_world.reconstruct_room_proxy_geometry`; the front face stands `front_depth_m` behind the source camera). Each hit is projected into the source camera and sampled bilinearly. Every pixel is labelled `observed`, `outside_source_frustum` or `performer_occluded`. Performers are not on the proxy, so a mask removes them instead of smearing them over the walls. Observed pixels supplied at more than four output pixels per source pixel are flagged `stretched`.
3. **Ledger.** `build_ledger` counts statuses per room face and records a hash of the geometry. `fill_unobserved(..., "face_mean")` offers a placeholder; filled pixels remain unobserved and the ledger says so.

Acceptance of any reverse-angle image stays pending. Generated listeners, lip-sync trials and mouth versions are separate studies; this tool only fixes which pixels are real.

## Run

```bash
python tools/reverse_angle.py --demo outputs/reverse-demo          # synthetic room, no media
python tools/reverse_angle.py --frame frame.png --proxy proxy.json \
    --performer-a 0 0 2.5 --performer-b 0.2 0 4.2 --subject-mask mask.png --output outputs/reverse
```

Performer positions are in the proxy's metre frame (x right, y up, z forward from the source camera). A blocked request prints a JSON error and exits 2 without creating the output directory.

## Verification

Tests render the same synthetic room from both cameras and compare reconstructed pixels with the true reverse render: mean absolute error under 2.5 of 255 on well-sampled observed pixels, the front wall never observed, same-camera reprojection exact, deterministic hashes. A narrow lens leaves almost nothing in common between opposite views (the narrow-lens case was measured as zero observed pixels), so fixtures use a 100 degree field of view.
