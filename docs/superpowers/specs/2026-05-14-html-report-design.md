# HTML report export for prototype-feedback — design

**Date:** 2026-05-14
**Scope:** prototype-feedback-extension (reviewer-side generator) + prototype-feedback-server (owner-side generator)
**Schema:** additive on v3 — no version bump

## Problem

The v3 ZIP export is Claude-shaped: a slim JSON plus separate PNGs that Claude Code can `Read`. But the reviewer often wants to share feedback with a non-technical stakeholder (designer, PM, client) who isn't going to unzip and read JSON. The reference artifact at `C:\Users\sebas\Downloads\auth-prototype-feedback.html` shows the desired shape — a single-file HTML report, Apple-styled, with a numbered comment card per feedback item, page-context screenshot on the left, comment text + meta + pin detail on the right.

## Goal

Add an HTML report artifact that opens anywhere offline, emails as one attachment, and matches the reference file's structure and aesthetic. Generators live on both the reviewer side (extension popup) and the owner side (server dashboard), producing identical HTML.

Non-goals (v1): editing the report after generation; per-comment threaded replies; multi-reviewer aggregation across batches (server's existing batch-per-reviewer model stays); print stylesheet; light/dark theme toggle; in-report comments search/filter.

## Output

- **Filename:** `prototype-feedback_<reviewer>_<ts>.html` (mirrors ZIP naming).
- **Format:** single HTML file with inlined CSS and base64-encoded images in `<img src="data:image/png;base64,…">`. No external assets, no JS required.
- **Title:** `${project || 'Prototype'} — Feedback Report`.
- **Size budget:** ~150–300 KB per comment after viewport downscale. Five-comment session ≈ 1 MB.

## HTML structure

Mirrors `~/Downloads/auth-prototype-feedback.html` exactly. The CSS block lives verbatim in [Appendix A](#appendix-a--canonical-css).

```
<header class="report-header">
  <h1>${title}</h1>
  <div class="report-meta">
    <span>Reviewer: <strong>${reviewer}</strong></span>
    <span>Generated: <strong>${exported_at}</strong></span>
    <span>Source: <strong>${comment_count} comment(s)</strong></span>
  </div>
</header>

<main class="comments-container">

  <section class="summary-bar">
    <div class="summary-stat"><div class="value">${total}</div><div class="key">Total comments</div></div>
    <div class="summary-divider"></div>
    <div class="summary-stat"><div class="value">${pages}</div><div class="key">Pages reviewed</div></div>
    <div class="summary-divider"></div>
    <div class="summary-stat"><div class="value">${elementPins}</div><div class="key">Element pins</div></div>
    <div class="summary-divider"></div>
    <div class="summary-stat"><div class="value">${regionPins}</div><div class="key">Region pins</div></div>
  </section>

  {# Console section only rendered when console_log.length > 0 #}
  <section class="console-section">
    <details>
      <summary>⚠ Console errors and warnings (${consoleLog.length})</summary>
      <ul class="console-list">
        {% for entry in console_log %}
          <li>
            <time>${HH:MM:SS}</time>
            <span class="level-pill level-${entry.level}">${entry.level}</span>
            <span class="msg">${entry.message}</span>
            <span class="origin">${entry.source}:${entry.line}</span>
          </li>
        {% endfor %}
      </ul>
    </details>
  </section>

  {% for c in comments %}
    <article class="comment-card">
      <header class="comment-header">
        <div class="comment-number">${i+1}</div>
        <div class="comment-page">
          <a href="${c.page_url}" target="_blank">↗ Open page</a>
        </div>
        <div class="comment-timestamp">${formatted_ts}</div>
      </header>
      <div class="comment-body">
        <div class="comment-screenshot">
          <img src="${viewport_data_url || crop_data_url}" alt="">
        </div>
        <div class="comment-content">
          <div class="comment-text">${c.comment}</div>
          <div class="comment-meta">
            <span class="meta-tag ${c.region_type}">
              <span class="label">Type:</span> ${c.region_type === 'element' ? 'Element pin' : 'Region pin'}
            </span>
            <span class="meta-tag"><span class="label">Page:</span> ${pathname}</span>
            {% if params %}
              <span class="meta-tag"><span class="label">Params:</span> ${params_joined}</span>
            {% endif %}
            {% if c.region_type === 'region' %}
              <span class="meta-tag">
                <span class="label">Bbox:</span> x=${c.bbox.x}, y=${c.bbox.y}, w=${c.bbox.w}, h=${c.bbox.h}
              </span>
            {% endif %}
          </div>
          {% if c.selector %}
            <div class="selector-tag">${c.selector}</div>
          {% endif %}
          {% if c.attachments.length > 0 %}
            <div class="attachments-row">
              {% for a in c.attachments %}
                {% if a.type === 'link' %}
                  <a class="attachment-chip link" href="${a.url}" target="_blank">${kindIcon} ${a.title || a.url}</a>
                {% elif a.type === 'image' %}
                  <a class="attachment-chip image" href="${data_url}" target="_blank">
                    <img src="${data_url}" alt="${a.name}">
                  </a>
                {% elif a.type === 'file' %}
                  <a class="attachment-chip file" href="${data_url}" download="${a.name}">📎 ${a.name}</a>
                {% endif %}
              {% endfor %}
            </div>
          {% endif %}
          <div class="pin-detail">
            <div class="pin-detail-label">${c.region_type === 'element' ? 'Pinned element' : 'Pinned region'}</div>
            <img src="${crop_data_url}" alt="">
          </div>
        </div>
      </div>
    </article>
  {% endfor %}

</main>
```

## Schema delta (additive on v3)

A new optional field on each comment: `viewport_screenshot_path` (e.g. `viewport-screenshots/<comment-id>.png`). When present in the v3 ZIP, the HTML report's left panel uses it; when absent, falls back to `screenshot_path`. Old v3 ZIPs without this field remain valid. Schema string stays `prototype-feedback/v3`.

### Extension capture changes

`captureAndStore()` in `background.js` already produces a full viewport PNG (`dataUrl` from `chrome.tabs.captureVisibleTab`) before cropping to `bbox`. Currently that full image is discarded after the crop. The change:

1. Downscale the viewport bitmap to max 1200 px wide using `OffscreenCanvas` (preserves aspect ratio).
2. Encode to PNG, base64.
3. Persist alongside the existing `screenshot_png_b64` as `viewport_png_b64` on the stored comment entry.

### Extension export changes

`buildExportDownload()` adds a new ZIP entry per comment that has a viewport image:

```
viewport-screenshots/<comment-id>.png
```

Slim JSON's comment gets `viewport_screenshot_path: "viewport-screenshots/<id>.png"`.

### Server ingest changes

`ingest_payload` in `main.py` accepts `viewport_screenshot_path` from the unzipped temp dir and copies it into `data/batches/<batch-id>/viewport-screenshots/<comment-id>.png`. Database `comments` table gains an optional `viewport_path` column (single ALTER, NULLable, no migration drama). Existing rows have NULL — server's HTML renderer falls back to the crop in that case.

## Generators

### Extension (reviewer side)

New file: `extension/lib/report-template.js` — exports a single function:

```js
function buildHtmlReport({ title, reviewer, exported_at, comments, console_log }) → string
```

Implementation is a tagged-template-literal returning the full HTML document. CSS is embedded verbatim from Appendix A. The function takes resolved data URLs in the input — the caller (in `background.js`) is responsible for converting `screenshot_png_b64` → `data:image/png;base64,${b64}` before invocation.

`background.js` gains:

- A new message handler: `case 'export_html'` → calls `buildHtmlReportFromStorage()` → produces the HTML string → encodes UTF-8 → base64 → triggers `chrome.downloads.download` with `data:text/html;base64,...`.
- The handler is similar in shape to `buildExportDownload()` but produces a single HTML string instead of a ZIP.

`popup.html` adds an "Export HTML" button next to the existing "Export" button. `popup.js` wires its click handler to send the `export_html` message.

### Server (owner side)

New file: `server/templates/report.html.j2` — Jinja2 mirror of the same HTML structure with the same CSS verbatim.

New endpoint:

```python
@app.get("/batch/{batch_id}/report.html")
def batch_html_report(batch_id: str): ...
```

Loads the batch from the DB, loads each comment's screenshot + viewport (reads bytes from disk, base64-encodes inline), feeds the Jinja2 template, returns `HTMLResponse(text)`.

Dashboard's batch row gets a new link: `<a href="/batch/{{batch.id}}/report.html" target="_blank">📄 Report</a>` next to the existing Claude prompt/JSON links.

## Page URL parsing

Both generators produce the same `Page:` and `Params:` meta tags from the comment's `page_url`:

```js
const u = new URL(c.page_url);
const pathname = u.pathname;        // → "/prototypes/start"
const params = [...u.searchParams.entries()]
  .map(([k, v]) => `${k}=${v}`)
  .join(', ');                       // → "persona=admin, auth=federated"
```

`Params:` tag is rendered only when `params.length > 0`.

## Time format

`May 13, 2026 · 6:11 PM` — matches the reference file exactly.

- Extension: `new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric' }).format(d)` + ` · ` + `new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit', hour12: true }).format(d)`. Joined with the middle-dot ` · `.
- Server: `strftime('%b %-d, %Y · %-I:%M %p')` (Linux/WSL); Windows requires `%#d`/`%#I`. Wrap in a helper that picks per-platform.

## Edge cases

| Case | Behavior |
|---|---|
| No comments | Report renders header + summary bar (all zeros) + a `<p class="empty">No comments captured yet.</p>` in place of the cards. |
| No `viewport_png_b64` / `viewport_screenshot_path` | Left panel uses the cropped image (same as Option A in brainstorming). |
| Comment with no attachments | `.attachments-row` block omitted. |
| Console log empty | `.console-section` block omitted entirely. |
| `page_url` not parseable as URL | Show raw `page_url` in the page link; skip `Page:` and `Params:` tags. |
| Region pin (no selector) | `.selector-tag` block omitted; `Bbox:` meta tag rendered. |
| Element pin (has selector) | `.selector-tag` rendered; `Bbox:` meta tag omitted. |
| Attachment with `data_b64`/inline (v2 legacy) | Server only — embed as data URL anyway. Extension only ships v3 data. |

## Files touched

| File | Status | Change |
|---|---|---|
| `extension/background.js` | Modify | Persist downscaled `viewport_png_b64`; new `export_html` message; externalize viewport PNG in ZIP |
| `extension/lib/report-template.js` | Create | `buildHtmlReport({…}) → string`; embeds CSS verbatim |
| `extension/popup.html` | Modify | Add "Export HTML" button |
| `extension/popup.js` | Modify | Wire button to `export_html` message |
| `extension/build.ps1` | Modify | `$required` adds `lib\report-template.js` |
| `extension/tools/test-report.js` | Create | Node test: feed fixture into `buildHtmlReport`, assert structure |
| `server/main.py` | Modify | `ingest_payload` consumes `viewport_screenshot_path`; new `/batch/<id>/report.html` route; new `viewport_path` column on `comments` |
| `server/templates/report.html.j2` | Create | Jinja2 mirror |
| `server/templates/index.html` | Modify | Batch row gains "Report" link |
| `server/main.py` (`init_db` migrations block, ~line 156) | Modify | Add `if not _column_exists(c, "comments", "viewport_path"): c.execute("ALTER TABLE comments ADD COLUMN viewport_path TEXT")` — mirrors the existing `project_id` / `reviewer_email` idempotent-migration pattern |

## Backward compatibility

- Old v2 JSON exports still ingest. Their batches have NULL `viewport_path`. Server HTML report falls back to crop in the left panel.
- Old v3 ZIPs without `viewport_screenshot_path` ingest normally. Same fallback.
- Existing extension users: comments captured before this ships have no `viewport_png_b64`. The HTML report uses the crop in the left panel for those comments. No data loss.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Two implementations of the same HTML drift apart | CSS lives in this spec verbatim (Appendix A). Both impls paste it. Comment-card structure is the same. If they drift, the cross-impl parity test catches it. |
| Inline base64 image bloat → giant HTML | Downscale viewport to 1200 px wide before encoding. Crop already at bbox size. Five-comment session ≈ 1 MB. |
| `chrome.downloads.download` data-URL size limit | Same as ZIP path; >2 MB historically iffy but modern Chrome handles up to ~32 MB. Document. If a user hits it, server-side report at `/batch/<id>/report.html` is the workaround. |
| Cross-platform `strftime` differences (Linux `%-d` vs Windows `%#d`) | Helper function picks the right token per `sys.platform`. |
| Hostile content in `comment` text → XSS in HTML | Both generators HTML-escape the comment text. Server uses Jinja2's autoescape (default on). Extension's template-literal manually wraps via an `escapeHtml()` helper applied to every interpolated value. |
| Console log entries with very long stacks → blow up the `<details>` block | Truncate displayed message + stack at 500 chars in the HTML view; full content is in the JSON. |

## Testing

- **Unit (extension):** `tools/test-report.js` — feed a 3-comment fixture (1 element, 1 region, 1 with attachments + console_log) into `buildHtmlReport()`. Assert output contains expected substrings (`<h1>`, all 3 `comment-card` blocks, console section, summary numbers correct).
- **Unit (server):** Pytest hitting `/batch/<id>/report.html` with a known batch fixture; assert `200`, MIME `text/html`, expected substring count.
- **Visual:** Generate one report from extension fixture, open in Chrome, eyeball against `~/Downloads/auth-prototype-feedback.html`.
- **Cross-impl parity (deferred, optional v2):** Node harness loads both templates with the same fixture data, normalizes whitespace, diffs. Catches drift early. Not required for v1 ship.

## Out of scope (v2 candidates)

- Multi-reviewer aggregation report (one HTML covering N batches for the same project)
- Reply threads inside the report
- Filter/search UI in the HTML
- Light/dark theme toggle
- Print stylesheet
- Click-to-zoom lightbox on screenshots (right now `<a target="_blank">` is the click-through)
- Full-page scroll-stitched screenshots
- Source-map resolution of console stack frames in the report

## Appendix A — Canonical CSS

This CSS is the single source of truth for both generators. Embedded verbatim from the reference file at `~/Downloads/auth-prototype-feedback.html` lines 7-119, with additions for `.console-section`, `.attachments-row`, `.attachment-chip`, and `.empty` callouts:

```css
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f5f7; color: #1d1d1f; }

.report-header { background: #1d1d1f; color: #f5f5f7; padding: 32px 48px; }
.report-header h1 { font-size: 28px; font-weight: 600; margin-bottom: 8px; }
.report-meta { display: flex; gap: 32px; font-size: 13px; color: #a1a1a6; margin-top: 12px; }
.report-meta span strong { color: #f5f5f7; }

.comments-container { max-width: 1100px; margin: 32px auto; padding: 0 24px; }

.comment-card {
  background: #fff; border-radius: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 4px 16px rgba(0,0,0,0.05);
  margin-bottom: 28px; overflow: hidden;
}

.comment-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 24px; border-bottom: 1px solid #f0f0f0; background: #fafafa;
}
.comment-number {
  width: 28px; height: 28px; border-radius: 50%;
  background: #0071e3; color: #fff;
  font-size: 13px; font-weight: 600;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.comment-page { font-size: 12px; color: #6e6e73; margin-left: 12px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.comment-page a { color: #0071e3; text-decoration: none; }
.comment-page a:hover { text-decoration: underline; }
.comment-timestamp { font-size: 11px; color: #a1a1a6; white-space: nowrap; margin-left: 16px; }

.comment-body { display: flex; }

.comment-screenshot {
  flex-shrink: 0; width: 360px; background: #f5f5f7;
  border-right: 1px solid #f0f0f0;
  display: flex; align-items: flex-start; justify-content: center; padding: 16px;
}
.comment-screenshot img {
  max-width: 100%; max-height: 320px; object-fit: contain;
  border-radius: 6px; box-shadow: 0 1px 4px rgba(0,0,0,0.15);
}

.comment-content { flex: 1; padding: 24px; display: flex; flex-direction: column; gap: 16px; }

.comment-text { font-size: 15px; line-height: 1.6; color: #1d1d1f; white-space: pre-wrap; }

.comment-meta { display: flex; flex-wrap: wrap; gap: 8px; }
.meta-tag {
  display: inline-flex; align-items: center; gap: 4px;
  background: #f5f5f7; border: 1px solid #e5e5ea;
  border-radius: 6px; padding: 4px 10px;
  font-size: 11px; color: #6e6e73;
}
.meta-tag .label { font-weight: 600; color: #1d1d1f; }
.meta-tag.region { background: #fff3e0; border-color: #ffb74d; }
.meta-tag.element { background: #e8f5e9; border-color: #81c784; }

.selector-tag {
  background: #f0f4ff; border: 1px solid #c5cff5;
  border-radius: 6px; padding: 6px 10px;
  font-size: 11px; font-family: "SF Mono", Menlo, monospace;
  color: #3451b2; word-break: break-all;
}

.pin-detail {
  background: #f9f9fb; border: 1px solid #e5e5ea; border-radius: 8px;
  padding: 12px; display: flex; flex-direction: column; gap: 8px;
}
.pin-detail-label {
  font-size: 11px; font-weight: 600; color: #6e6e73;
  text-transform: uppercase; letter-spacing: 0.05em;
}
.pin-detail img {
  max-width: 100%; border-radius: 4px;
  border: 1px solid #e0e0e0; box-shadow: 0 1px 3px rgba(0,0,0,0.1);
}

.summary-bar {
  background: #fff; border-radius: 12px; padding: 20px 24px; margin-bottom: 28px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  display: flex; gap: 32px; align-items: center;
}
.summary-stat { text-align: center; }
.summary-stat .value { font-size: 28px; font-weight: 700; color: #0071e3; }
.summary-stat .key { font-size: 12px; color: #6e6e73; margin-top: 2px; }
.summary-divider { width: 1px; height: 40px; background: #e5e5ea; }

/* NEW — console section */
.console-section {
  background: #fff; border-radius: 12px; padding: 16px 24px; margin-bottom: 28px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
.console-section summary {
  cursor: pointer; font-size: 14px; font-weight: 600; color: #1d1d1f;
  list-style: none; padding: 4px 0;
}
.console-section summary::-webkit-details-marker { display: none; }
.console-list { list-style: none; padding-top: 12px; }
.console-list li {
  display: flex; gap: 10px; padding: 8px 0;
  border-top: 1px solid #f0f0f0; font-size: 12px; align-items: baseline;
}
.console-list time { color: #a1a1a6; font-family: "SF Mono", Menlo, monospace; flex-shrink: 0; }
.console-list .level-pill {
  text-transform: uppercase; font-size: 10px; font-weight: 600; letter-spacing: 0.05em;
  padding: 2px 6px; border-radius: 4px; flex-shrink: 0;
}
.console-list .level-error { background: #fde2e2; color: #b91c1c; }
.console-list .level-warn { background: #fef3c7; color: #92400e; }
.console-list .msg { flex: 1; color: #1d1d1f; word-break: break-word; }
.console-list .origin {
  font-family: "SF Mono", Menlo, monospace; color: #6e6e73; font-size: 11px; flex-shrink: 0;
}

/* NEW — attachments row */
.attachments-row { display: flex; flex-wrap: wrap; gap: 8px; }
.attachment-chip {
  display: inline-flex; align-items: center; gap: 6px;
  background: #f5f5f7; border: 1px solid #e5e5ea;
  border-radius: 6px; padding: 6px 10px;
  font-size: 12px; color: #1d1d1f; text-decoration: none;
}
.attachment-chip:hover { background: #ececec; }
.attachment-chip.link { background: #e8f0ff; border-color: #c5cff5; color: #3451b2; }
.attachment-chip.image img {
  width: 64px; height: 64px; object-fit: cover; border-radius: 4px;
}
.attachment-chip.file { background: #fff8e1; border-color: #ffe0a3; }

/* NEW — empty state */
.empty { text-align: center; padding: 48px; color: #6e6e73; font-size: 14px; }

@media (max-width: 700px) {
  .comment-body { flex-direction: column; }
  .comment-screenshot { width: 100%; border-right: none; border-bottom: 1px solid #f0f0f0; }
  .report-header { padding: 20px 16px; }
  .report-meta { flex-wrap: wrap; gap: 12px; }
}
```
