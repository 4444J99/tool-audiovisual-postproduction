# Horizontal and vertical layout comparison: geometry

`tools/render-layout-comparison.mjs` renders eight layouts in a 768 x 432 horizontal and a 432 x 768 vertical canvas. Its placement rules now live in `web/fx/layout-geometry.mjs`, a pure module with no canvas or media. The renderer draws from it; `tools/analyze-layout-geometry.mjs` measures it; `tests/fx/layout-geometry.test.mjs` proves the extracted rules equal the former inline arithmetic for every layout, region, orientation and 40 instants (3,200 placements) and keeps the fixed band, anchor and caption rectangles at their original values.

The refactor was also checked end to end: the old and new renderers, run on the same media pack, produced byte-identical poster frames (frame 72) for all 16 layout/orientation renders. That check used a synthetic pack, not the film.

```bash
node tools/analyze-layout-geometry.mjs --table            # readable table
node tools/analyze-layout-geometry.mjs outputs/layout-geometry.json
```

## Measured result (24 instants over 12.167 s; shares of the canvas, means)

| layout | orientation | anchor | silhouettes | visible beside anchor | hidden by anchor | caption underlaid | regions drawn (min) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| unfolded-room | horizontal | 21.7% | 53.9% | 42.3% | 21.5% | 71.1% | 5 |
| unfolded-room | vertical | 25.5% | 56.7% | 56.7% | 0.0% | 95.3% | 5 |
| five-territories | horizontal | 21.7% | 54.4% | 47.1% | 13.5% | 0.9% | 5 |
| five-territories | vertical | 25.5% | 62.9% | 57.8% | 8.1% | 98.2% | 5 |
| sender-channel-receiver | horizontal | 21.7% | 53.8% | 46.0% | 14.5% | 4.3% | 5 |
| sender-channel-receiver | vertical | 25.5% | 61.5% | 56.3% | 8.4% | 93.8% | 5 |
| opposing-angle-duplex | horizontal | 21.7% | 54.4% | 47.1% | 13.5% | 0.9% | 5 |
| opposing-angle-duplex | vertical | 25.5% | 62.9% | 57.8% | 8.1% | 98.2% | 5 |
| face-body-world | horizontal | 21.7% | 50.6% | 34.9% | 31.0% | 0.0% | 5 |
| face-body-world | vertical | 25.5% | 58.7% | 43.9% | 25.3% | 68.5% | 5 |
| travelling-attention | horizontal | 21.7% | 53.0% | 45.1% | 15.0% | 5.0% | 5 |
| travelling-attention | vertical | 25.5% | 60.4% | 55.3% | 8.5% | 93.3% | 5 |
| temporal-accumulation | horizontal | 21.7% | 63.1% | 52.0% | 17.6% | 9.9% | 5 |
| temporal-accumulation | vertical | 25.5% | 69.4% | 61.9% | 10.8% | 100.0% | 5 |
| architecture-of-vacancy | horizontal | 21.7% | 43.8% | 38.0% | 13.2% | 0.6% | 4 |
| architecture-of-vacancy | vertical | 25.5% | 51.7% | 47.7% | 8.0% | 79.8% | 4 |

Reading the numbers:

- The complete reference (the anchor) takes 21.7% of the horizontal canvas and 25.5% of the vertical one, so the vertical format gives the original picture slightly more weight in every layout.
- In the vertical canvas the silhouette boxes lie under 68-100% of the caption rectangle in all eight layouts, against 0-10% in the horizontal canvas for seven layouts (71% for unfolded-room). The caption layer is drawn over the silhouettes at 85% opacity, so this measures how much figure sits beneath the words, which bears on legibility in the vertical edition. The measure is rectangle occupancy, not rendered contrast; legibility needs a rendered check on the real media.
- `five-territories` and `opposing-angle-duplex` have identical rectangles. They differ only by mirroring odd regions, so geometry alone cannot separate them.
- `architecture-of-vacancy` always vacates exactly one region, so at least four are drawn at every instant.

The measurements describe rectangle placement only. They make no claim about how the figures read in the rendered films, and artistic acceptance stays pending. Rendering the films still requires the private asset pack.
