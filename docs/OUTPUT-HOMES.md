# Rendered outputs and where they live

Large renders are not committed (`outputs/` is git-ignored). Each durable copy is recorded here with its checksum.

## Macbeth read-through (2026-11-15)

- File: `macbeth-readthrough.mp4`, H.264 1080x1920 plus AAC, 21.03 s, 1,515,073 bytes.
- SHA-256: `70f0cbe5b68361befee9ca7647831b009547a1ba84b091a8591ebfb2e9b9b761` (MD5 `4e0a75d904affa1985967e640e3ae21d`, the value Drive reports).
- Durable home: Google Drive, `gdrive:Projects/tool-audiovisual-postproduction/macbeth-readthrough-20261115/` (`macbeth-readthrough.mp4` and `timing.json`), copied with `rclone copy` on 2026-11-16 and the MD5 matched against the local file.
- Working copy: `custody-staging/macbeth-readthrough-20261115/` (also holds `rough.wav`, the frames and the art). The audio is the owner's own read-through, so the Drive folder is private and the file is not attached to a public release.
