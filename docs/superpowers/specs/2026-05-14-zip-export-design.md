# ZIP export for prototype-feedback — design

**Date:** 2026-05-14
**Scope:** prototype-feedback-extension (export pipeline) + prototype-feedback-server (ingest)
**Schema bump:** v2 → v3

## Problem

The extension exports captured comments as one pretty-printed JSON file. Each comment's cropped screenshot is base64-inlined as `screenshot_png_b64`, producing a single ~44 KB line per comment. Claude Code's `Read` tool errors on any line that exceeds ~25 000 tokens, so when the JSON is fed to Claude, every screenshot field is unreadable. Claude phrases this as "the base64 was truncated."

Larger image/file attachments (capped at 5 MB) have the same problem.

The local server's slim-output pipeline already solves this (extracts assets to disk, writes a slim `claude_input.json` with paths), but most direct extension→Claude workflows skip the server entirely.

## Goal

Produce an export artifact that Claude Code can `Read` reliably regardless of comment count, image size, or whether the local server is in the loop.

Non-goals: re-architecting capture, edit, or storage. Internal `chrome.storage.local` shape is unchanged.

## Output format

Single ZIP file, replacing today's JSON export:

```
prototype-feedback_<reviewer>_<ts>.zip
├── feedback.json                          (slim, schema "prototype-feedback/v3")
├── screenshots/
│   └── <comment-id>.png                   (one cropped PNG per comment)
└── attachments/
    └── <comment-id>/
        └── <safe-filename>                (one file per image/file attachment)
```

- **STORED**-only zip (no DEFLATE). PNGs are already compressed; user-attached files are usually small. STORED keeps the implementation ~80 lines and dependency-free.
- A new `lib/zip.js` in the extension implements zip writing, vendored-style (matches the existing `lib/finder.js` pattern).

## Schema v3 (`feedback.json`)

Diff from v2:

| Field | v2 | v3 |
|---|---|---|
| `comments[].screenshot_png_b64` | base64 string | **removed** |
| `comments[].screenshot_path` | — | `"screenshots/<id>.png"` |
| `comments[].attachments[type=image\|file].data_b64` | base64 string | **removed** |
| `comments[].attachments[type=image\|file].path` | — | `"attachments/<id>/<name>"` |
| `comments[].attachments[type=link]` | unchanged | unchanged |
| `schema` | `prototype-feedback/v2` | `prototype-feedback/v3` |

`_llm_guide` rewritten: paths are relative to the unzip root; instruct Claude to call `Read` on `screenshot_path` directly. The "How to extract base64" note from v2 is removed.

All other comment fields (`id`, `created_at`, `reviewer`, `page_url`, `selector`, `bbox`, `viewport`, `region_type`, `comment`) unchanged.

## Extension changes

- `background.js`
  - `SCHEMA` bump to `prototype-feedback/v3`.
  - `captureAndStore()` unchanged — internal storage keeps `screenshot_png_b64` so the popup thumbnail continues to render. Externalization happens at export only.
  - `buildExportDownload()` rewritten:
    1. Load comments + settings.
    2. Build the slim payload (paths instead of base64).
    3. For each comment: decode `screenshot_png_b64` → bytes; decode each `attachments[].data_b64` → bytes; collect file entries.
    4. Pass the file entries to `lib/zip.js#buildZip()`, which returns a `Uint8Array`.
    5. Create a blob URL (`URL.createObjectURL(new Blob([zipBytes], {type: 'application/zip'}))`) and pass to `chrome.downloads.download` with `.zip` filename.
  - `llmGuide()` returns v3 text.
- `lib/zip.js` (new, ~80 lines)
  - `buildZip(entries: {path: string, bytes: Uint8Array}[]) → Uint8Array`
  - Implements STORED-only local file headers, central directory, end-of-central-directory record. No compression, no encryption, no zip64.
- `manifest.json` — no changes (no new permissions needed; `downloads` already declared).
- `popup.js` — no functional change; the existing `export` button now triggers a ZIP download.

## Server changes

- `main.py`
  - `POST /api/ingest` accepts multipart upload with `.zip` content-type alongside the existing JSON path.
  - On ZIP receipt:
    1. Stream-unzip into a fresh `data/batches/<batch-id>/` directory.
    2. Parse `feedback.json`; validate `schema` is `prototype-feedback/v3`.
    3. Rewrite each `screenshot_path` / `attachments[].path` from relative to absolute (server-side path).
    4. Write `claude_input.json` and `claude_prompt.md` exactly as the v2 path does today.
  - Existing v1 + v2 JSON ingest path preserved unchanged for old exports in users' Downloads folders.
  - Detection: filename suffix `.zip` OR content-type `application/zip` → ZIP path; otherwise JSON path.
- Dashboard `templates/*.html` file input: `accept=".zip,.json"`.
- No DB schema change.

## Backward compatibility

- Old v2 JSON exports continue to ingest via the existing server path.
- Internal `chrome.storage.local` shape unchanged — no migration required for users with in-progress comments at the upgrade boundary.
- The two existing v2 JSON exports in `~/Downloads/` remain valid for server ingest.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Hand-rolled STORED zip has subtle byte-layout bugs | Test fixture: build a zip with 1 known PNG; verify `unzip -l` and `unzip` round-trip on Windows + Linux. |
| Large exports (many comments) hit Chrome download URL size limits via blob URL | Blob URLs aren't subject to the data-URL size cap; `chrome.downloads.download` handles large blobs fine. Spot-check with 20-comment export. |
| Schema v3 ZIP dropped on un-updated server returns 4xx | Acceptable for the transition window; both extension and server ship together. |
| `chrome.storage.local` quota pressure (b64 still stored internally) | Unchanged from today. If it ever becomes a real issue, separate task to move internal storage off b64. |
| Filename collision in `attachments/<id>/<name>` when two attachments share the same name | Disambiguate by prefixing index: `attachments/<id>/<i>-<name>`. |

## Testing plan

- **Unit (extension):** import `lib/zip.js` in a Node harness; build a zip from synthetic entries; assert byte structure (local-file-header magic, central-directory magic, EOCD record).
- **Integration (extension):** capture one element comment + one region comment with a 1 MB image attachment; click Export; unzip the resulting `.zip`; verify file layout and `Read` works on each PNG.
- **Integration (server):** drop the resulting ZIP on the dashboard; verify `data/batches/<id>/` is populated and `claude_input.json` points at the expected paths.
- **Regression:** drop one of the existing v2 JSON files in `~/Downloads/` on the dashboard; verify v2 path still works.
- **Cross-platform:** unzip on Windows (`Expand-Archive`) and Linux (`unzip`) — both succeed.

## Out of scope (deferred)

- Console / network capture (separate spec).
- DEFLATE compression in the zip.
- Zip64 support (unnecessary at single-reviewer export sizes).
- Server's slim-conversion code path consolidation (the v2 and v3 paths can share later if useful).
- Privacy/encryption — exports remain plaintext.
