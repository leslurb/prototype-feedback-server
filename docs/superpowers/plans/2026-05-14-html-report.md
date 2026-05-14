# HTML report export — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single-file HTML report export to prototype-feedback (extension + server) that mirrors `~/Downloads/auth-prototype-feedback.html` — Apple-styled report with numbered comment cards, viewport-context + pin-detail screenshots, console-log section, and stakeholder-friendly layout.

**Non-goals:**
- Multi-reviewer aggregation across batches
- Reply threads inside the report
- Filter/search UI in the rendered HTML
- Light/dark theme toggle
- Print stylesheet
- Click-to-zoom lightbox (`<a target="_blank">` suffices)
- Full-page scroll-stitched screenshots
- Source-map resolution of console stack frames
- Schema version bump (additive field only)

**Architecture:** Two generators sharing the same HTML structure. Extension's `lib/report-template.js` is a pure function returning an HTML string from resolved data URLs. Server's `templates/report.html.j2` is a Jinja2 mirror. Both embed the canonical CSS verbatim from the spec's Appendix A. A new optional `viewport_screenshot_path` field on v3 comments carries a downscaled (max 1200 px wide) viewport PNG for the report's left-panel "context" image; absence falls back to the cropped screenshot.

**Tech Stack:** Chrome MV3 (vanilla JS, `OffscreenCanvas`), Python 3 / FastAPI / Jinja2, SQLite (one ALTER).

**Assumptions / Preconditions:**
- `C:\Users\sebas\prototype-feedback-extension\` exists, loaded unpacked in Chrome.
- `C:\Users\sebas\prototype-feedback-server\` on branch `main`, working tree clean before start.
- Server stopped before edits, restarted after Task 8.
- `node --version` works on PATH.
- v3 ZIP export from the prior plan is already shipped (`lib/zip.js`, `inject/console-tap.js` exist; `feedback.json` schema is `prototype-feedback/v3`).
- Reference file at `C:\Users\sebas\Downloads\auth-prototype-feedback.html` is the visual ground truth — keep it open while implementing.
- Spec lives at `C:\Users\sebas\prototype-feedback-server\docs\superpowers\specs\2026-05-14-html-report-design.md` — Appendix A holds the canonical CSS to copy.

**Rollback:**
- Extension is not a git repo. Pre-existing snapshot `prototype-feedback-extension.bak-2026-05-14.zip` covers rollback to v0.2.0; take a fresh snapshot before Task 1 if you've shipped any other changes since.
- Server is a git repo. Revert with `git restore main.py templates/`. The `ALTER TABLE` adds a NULLable column — safe; explicit rollback is `ALTER TABLE comments DROP COLUMN viewport_path` (SQLite ≥ 3.35) or recreate the table (SQLite < 3.35).

---

## Task 1: Capture and persist the viewport screenshot

**Files:**
- Modify: `C:\Users\sebas\prototype-feedback-extension\background.js:53-99` (the `captureAndStore` function)

- [ ] **Step 1: Take a fresh snapshot of the extension dir**

Run:
```powershell
Compress-Archive -Path "C:\Users\sebas\prototype-feedback-extension\*" -DestinationPath "C:\Users\sebas\prototype-feedback-extension.bak-html-report.zip" -Force
```

Expected: file `prototype-feedback-extension.bak-html-report.zip` exists in `C:\Users\sebas\`, size > 100 KB.

- [ ] **Step 2: Add `viewport_png_b64` to the persisted comment entry**

In `background.js`, find the `captureAndStore` function. The full-viewport `bitmap` is already created from `chrome.tabs.captureVisibleTab` on line ~61, then cropped to `bbox`. Add a parallel downscale + encode of the full viewport. Replace the function body (everything inside `async function captureAndStore(payload, tab)`) with:

```js
async function captureAndStore(payload, tab) {
  const {
    bbox, dpr, viewport, page_url, page_title, selector, region_type,
    comment, attachments, reviewer, reviewer_email, project, project_id
  } = payload;

  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'png' });
  const fullBlob = await (await fetch(dataUrl)).blob();
  const bitmap = await createImageBitmap(fullBlob);

  // Cropped pin detail
  const sx = Math.max(0, Math.round(bbox.x * dpr));
  const sy = Math.max(0, Math.round(bbox.y * dpr));
  const sw = Math.max(1, Math.round(bbox.w * dpr));
  const sh = Math.max(1, Math.round(bbox.h * dpr));
  const cropCanvas = new OffscreenCanvas(sw, sh);
  cropCanvas.getContext('2d').drawImage(bitmap, sx, sy, sw, sh, 0, 0, sw, sh);
  const croppedBlob = await cropCanvas.convertToBlob({ type: 'image/png' });
  const screenshot_b64 = await blobToBase64(croppedBlob);

  // Viewport context (downscaled to max 1200 px wide for report bloat control)
  const MAX_W = 1200;
  const scale = Math.min(1, MAX_W / bitmap.width);
  const vw = Math.max(1, Math.round(bitmap.width * scale));
  const vh = Math.max(1, Math.round(bitmap.height * scale));
  const viewportCanvas = new OffscreenCanvas(vw, vh);
  viewportCanvas.getContext('2d').drawImage(bitmap, 0, 0, bitmap.width, bitmap.height, 0, 0, vw, vh);
  const viewportBlob = await viewportCanvas.convertToBlob({ type: 'image/png' });
  const viewport_b64 = await blobToBase64(viewportBlob);

  const id = crypto.randomUUID();
  const entry = {
    id,
    created_at: new Date().toISOString(),
    reviewer: reviewer || null,
    reviewer_email: reviewer_email || null,
    project: project || null,
    project_id: project_id || null,
    page_url,
    page_title,
    viewport,
    region_type,
    selector: selector || null,
    bbox,
    screenshot_png_b64: screenshot_b64,
    viewport_png_b64: viewport_b64,
    comment,
    attachments: Array.isArray(attachments) ? attachments : []
  };

  const data = await chrome.storage.local.get(STORAGE_KEY);
  const list = data[STORAGE_KEY] || [];
  list.push(entry);
  await chrome.storage.local.set({ [STORAGE_KEY]: list });
  await refreshBadge(list.length);
  return { id };
}
```

- [ ] **Step 3: Reload extension and verify in service-worker logs**

Open `chrome://extensions/`, reload "Prototype Feedback." Click "Inspect views: service worker." In the worker's devtools console, confirm no syntax/import errors.

- [ ] **Step 4: Capture one comment manually, inspect storage**

Visit any page (e.g., `https://example.com`), start a session, pin an element with comment "viewport test." In the service-worker console run:

```js
chrome.storage.local.get('comments', (d) => console.log(d.comments[0].viewport_png_b64?.length, d.comments[0].screenshot_png_b64?.length));
```

Expected: two non-zero numbers; viewport b64 length larger than the crop's. Delete the test comment via the popup's Clear when done.

---

## Task 2: Externalize viewport image in the ZIP export

**Files:**
- Modify: `C:\Users\sebas\prototype-feedback-extension\background.js` — the `buildExportDownload` function

- [ ] **Step 1: Add viewport entry to the zip and `viewport_screenshot_path` to the slim comment**

In `buildExportDownload`, find the block that handles `c.screenshot_png_b64`:

```js
    if (c.screenshot_png_b64) {
      const p = `screenshots/${c.id}.png`;
      zipEntries.push({ path: p, bytes: base64ToBytes(c.screenshot_png_b64) });
      slim.screenshot_path = p;
    }
```

Add the viewport sibling immediately after it:

```js
    if (c.viewport_png_b64) {
      const p = `viewport-screenshots/${c.id}.png`;
      zipEntries.push({ path: p, bytes: base64ToBytes(c.viewport_png_b64) });
      slim.viewport_screenshot_path = p;
    }
```

Also add `viewport_screenshot_path: null` to the `slim` object's initial declaration (right after `screenshot_path: null,`):

```js
      screenshot_path: null,
      viewport_screenshot_path: null,
      attachments: []
```

- [ ] **Step 2: Reload extension, export a ZIP, inspect contents**

After reload, capture a comment, click Export. Then:

```powershell
$zip = (Get-ChildItem "$env:USERPROFILE\Downloads\prototype-feedback_*.zip" | Sort LastWriteTime -Desc | Select -First 1).FullName
$out = "$env:TEMP\pfb-task2"; if (Test-Path $out) { Remove-Item -Recurse -Force $out }
Expand-Archive -Path $zip -DestinationPath $out -Force
Get-ChildItem -Recurse $out | ForEach-Object { $_.FullName.Replace("$out\", "") }
```

Expected: tree shows `feedback.json`, `screenshots/<id>.png`, AND `viewport-screenshots/<id>.png`. Reading `feedback.json` shows `viewport_screenshot_path: "viewport-screenshots/<id>.png"` on the comment.

---

## Task 3: Create `lib/report-template.js` with `buildHtmlReport()` + Node test

**Files:**
- Create: `C:\Users\sebas\prototype-feedback-extension\lib\report-template.js`
- Create: `C:\Users\sebas\prototype-feedback-extension\tools\test-report.js`

- [ ] **Step 1: Write the failing test**

Create `C:\Users\sebas\prototype-feedback-extension\tools\test-report.js`:

```js
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const code = fs.readFileSync(path.join(__dirname, '..', 'lib', 'report-template.js'), 'utf8');
const ctx = { console, URL };
ctx.globalThis = ctx;
ctx.self = ctx;
vm.createContext(ctx);
vm.runInContext(code, ctx);

const { buildHtmlReport } = ctx.PFBReport;
if (typeof buildHtmlReport !== 'function') throw new Error('PFBReport.buildHtmlReport not exposed');

const input = {
  title: 'Test Prototype — Feedback Report',
  reviewer: 'Alice',
  exportedAt: '2026-05-14T15:00:00.000Z',
  comments: [
    {
      id: 'c1',
      created_at: '2026-05-14T14:50:00.000Z',
      page_url: 'https://example.com/foo?a=1&b=two',
      region_type: 'element',
      selector: '#nav > a.primary',
      bbox: { x: 10, y: 20, w: 100, h: 30 },
      comment: 'Make this <bold>',
      screenshotDataUrl: 'data:image/png;base64,FAKE1',
      viewportDataUrl: 'data:image/png;base64,FAKE1V',
      attachments: []
    },
    {
      id: 'c2',
      created_at: '2026-05-14T14:55:00.000Z',
      page_url: 'https://example.com/bar',
      region_type: 'region',
      selector: null,
      bbox: { x: 5, y: 5, w: 50, h: 50 },
      comment: 'Pic',
      screenshotDataUrl: 'data:image/png;base64,FAKE2',
      viewportDataUrl: null,
      attachments: [
        { type: 'link', url: 'https://docs.google.com/document/abc', kind: 'google_doc', title: 'Spec' }
      ]
    }
  ],
  consoleLog: [
    { ts: '2026-05-14T14:51:00.000Z', level: 'error', kind: 'console', message: 'boom', source: 'a.js', line: 10 },
    { ts: '2026-05-14T14:52:00.000Z', level: 'warn',  kind: 'console', message: 'soft warn' }
  ]
};

const html = buildHtmlReport(input);

function assert(cond, msg) { if (!cond) throw new Error(`FAIL: ${msg}`); }

assert(html.startsWith('<!doctype html>') || html.startsWith('<!DOCTYPE html>'), 'has doctype');
assert(html.includes('<h1>Test Prototype — Feedback Report</h1>'), 'has title');
assert(html.includes('class="summary-stat"'), 'has summary bar');
assert(html.match(/class="comment-card"/g).length === 2, 'has 2 comment cards');
assert(html.includes('class="comment-number">1<'), 'numbered 1');
assert(html.includes('class="comment-number">2<'), 'numbered 2');
assert(html.includes('Make this &lt;bold&gt;'), 'comment text html-escaped');
assert(html.includes('#nav &gt; a.primary'), 'selector html-escaped');
assert(html.includes('Page:'), 'page meta tag');
assert(html.includes('/foo'), 'pathname rendered');
assert(html.includes('a=1, b=two'), 'params rendered');
assert(html.includes('Bbox:'), 'region bbox tag');
assert(html.includes('class="console-section"'), 'has console section');
assert(html.includes('Console errors and warnings (2)'), 'console count');
assert(html.includes('level-pill level-error'), 'error level pill');
assert(html.includes('level-pill level-warn'), 'warn level pill');
assert(html.includes('a.js:10'), 'console origin');
assert(html.includes('data:image/png;base64,FAKE1V'), 'viewport image used when present');
assert(html.includes('attachment-chip link'), 'link attachment rendered');
assert(html.includes('href="https://docs.google.com/document/abc"'), 'link href present');

// c2 has no viewportDataUrl → fallback to screenshot in left panel
const c2Start = html.indexOf('class="comment-number">2<');
const c2Slice = html.slice(c2Start, c2Start + 4000);
assert(c2Slice.includes('data:image/png;base64,FAKE2'), 'c2 falls back to crop in left panel');

console.log('PASS — buildHtmlReport structure verified');
```

- [ ] **Step 2: Run test, confirm RED**

```powershell
cd C:\Users\sebas\prototype-feedback-extension; node tools\test-report.js
```

Expected: ENOENT error referencing `lib\report-template.js`.

- [ ] **Step 3: Create `lib/report-template.js`**

Create `C:\Users\sebas\prototype-feedback-extension\lib\report-template.js`. Paste the entire file below (long — but every line is needed). The CSS string is copied verbatim from the spec's Appendix A.

```js
// Pure HTML-report generator for Prototype Feedback.
// Caller MUST resolve image base64 → data URLs BEFORE invoking buildHtmlReport.
// Exposes: globalThis.PFBReport = { buildHtmlReport } (browser/service-worker)
//          module.exports = { buildHtmlReport } (Node)
(function () {
  const CSS = `* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f5f7; color: #1d1d1f; }
.report-header { background: #1d1d1f; color: #f5f5f7; padding: 32px 48px; }
.report-header h1 { font-size: 28px; font-weight: 600; margin-bottom: 8px; }
.report-meta { display: flex; gap: 32px; font-size: 13px; color: #a1a1a6; margin-top: 12px; }
.report-meta span strong { color: #f5f5f7; }
.comments-container { max-width: 1100px; margin: 32px auto; padding: 0 24px; }
.comment-card { background: #fff; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 4px 16px rgba(0,0,0,0.05); margin-bottom: 28px; overflow: hidden; }
.comment-header { display: flex; align-items: center; justify-content: space-between; padding: 16px 24px; border-bottom: 1px solid #f0f0f0; background: #fafafa; }
.comment-number { width: 28px; height: 28px; border-radius: 50%; background: #0071e3; color: #fff; font-size: 13px; font-weight: 600; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.comment-page { font-size: 12px; color: #6e6e73; margin-left: 12px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.comment-page a { color: #0071e3; text-decoration: none; }
.comment-page a:hover { text-decoration: underline; }
.comment-timestamp { font-size: 11px; color: #a1a1a6; white-space: nowrap; margin-left: 16px; }
.comment-body { display: flex; }
.comment-screenshot { flex-shrink: 0; width: 360px; background: #f5f5f7; border-right: 1px solid #f0f0f0; display: flex; align-items: flex-start; justify-content: center; padding: 16px; }
.comment-screenshot img { max-width: 100%; max-height: 320px; object-fit: contain; border-radius: 6px; box-shadow: 0 1px 4px rgba(0,0,0,0.15); }
.comment-content { flex: 1; padding: 24px; display: flex; flex-direction: column; gap: 16px; }
.comment-text { font-size: 15px; line-height: 1.6; color: #1d1d1f; white-space: pre-wrap; }
.comment-meta { display: flex; flex-wrap: wrap; gap: 8px; }
.meta-tag { display: inline-flex; align-items: center; gap: 4px; background: #f5f5f7; border: 1px solid #e5e5ea; border-radius: 6px; padding: 4px 10px; font-size: 11px; color: #6e6e73; }
.meta-tag .label { font-weight: 600; color: #1d1d1f; }
.meta-tag.region { background: #fff3e0; border-color: #ffb74d; }
.meta-tag.element { background: #e8f5e9; border-color: #81c784; }
.selector-tag { background: #f0f4ff; border: 1px solid #c5cff5; border-radius: 6px; padding: 6px 10px; font-size: 11px; font-family: "SF Mono", Menlo, monospace; color: #3451b2; word-break: break-all; }
.pin-detail { background: #f9f9fb; border: 1px solid #e5e5ea; border-radius: 8px; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
.pin-detail-label { font-size: 11px; font-weight: 600; color: #6e6e73; text-transform: uppercase; letter-spacing: 0.05em; }
.pin-detail img { max-width: 100%; border-radius: 4px; border: 1px solid #e0e0e0; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.summary-bar { background: #fff; border-radius: 12px; padding: 20px 24px; margin-bottom: 28px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); display: flex; gap: 32px; align-items: center; }
.summary-stat { text-align: center; }
.summary-stat .value { font-size: 28px; font-weight: 700; color: #0071e3; }
.summary-stat .key { font-size: 12px; color: #6e6e73; margin-top: 2px; }
.summary-divider { width: 1px; height: 40px; background: #e5e5ea; }
.console-section { background: #fff; border-radius: 12px; padding: 16px 24px; margin-bottom: 28px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.console-section summary { cursor: pointer; font-size: 14px; font-weight: 600; color: #1d1d1f; list-style: none; padding: 4px 0; }
.console-section summary::-webkit-details-marker { display: none; }
.console-list { list-style: none; padding-top: 12px; }
.console-list li { display: flex; gap: 10px; padding: 8px 0; border-top: 1px solid #f0f0f0; font-size: 12px; align-items: baseline; }
.console-list time { color: #a1a1a6; font-family: "SF Mono", Menlo, monospace; flex-shrink: 0; }
.console-list .level-pill { text-transform: uppercase; font-size: 10px; font-weight: 600; letter-spacing: 0.05em; padding: 2px 6px; border-radius: 4px; flex-shrink: 0; }
.console-list .level-error { background: #fde2e2; color: #b91c1c; }
.console-list .level-warn { background: #fef3c7; color: #92400e; }
.console-list .msg { flex: 1; color: #1d1d1f; word-break: break-word; }
.console-list .origin { font-family: "SF Mono", Menlo, monospace; color: #6e6e73; font-size: 11px; flex-shrink: 0; }
.attachments-row { display: flex; flex-wrap: wrap; gap: 8px; }
.attachment-chip { display: inline-flex; align-items: center; gap: 6px; background: #f5f5f7; border: 1px solid #e5e5ea; border-radius: 6px; padding: 6px 10px; font-size: 12px; color: #1d1d1f; text-decoration: none; }
.attachment-chip:hover { background: #ececec; }
.attachment-chip.link { background: #e8f0ff; border-color: #c5cff5; color: #3451b2; }
.attachment-chip.image img { width: 64px; height: 64px; object-fit: cover; border-radius: 4px; }
.attachment-chip.file { background: #fff8e1; border-color: #ffe0a3; }
.empty { text-align: center; padding: 48px; color: #6e6e73; font-size: 14px; }
@media (max-width: 700px) {
  .comment-body { flex-direction: column; }
  .comment-screenshot { width: 100%; border-right: none; border-bottom: 1px solid #f0f0f0; }
  .report-header { padding: 20px 16px; }
  .report-meta { flex-wrap: wrap; gap: 12px; }
}`;

  const KIND_ICON = {
    google_doc: '📄', google_sheet: '📊', google_slide: '📑', google_form: '📝',
    google_docs: '📁', google_drive: '🗂', figma: '🎨', notion: '📓',
    github: '💻', loom: '🎥', miro: '🧩', link: '🔗'
  };

  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  function formatTime(iso) {
    try {
      const d = new Date(iso);
      const datePart = new Intl.DateTimeFormat('en-US', {
        month: 'short', day: 'numeric', year: 'numeric'
      }).format(d);
      const timePart = new Intl.DateTimeFormat('en-US', {
        hour: 'numeric', minute: '2-digit', hour12: true
      }).format(d);
      return `${datePart} · ${timePart}`;
    } catch (_) { return iso || ''; }
  }

  function formatClock(iso) {
    try {
      const d = new Date(iso);
      return d.toISOString().slice(11, 19);
    } catch (_) { return ''; }
  }

  function parsePageUrl(url) {
    try {
      const u = new URL(url);
      const pathname = u.pathname || '/';
      const params = [...u.searchParams.entries()]
        .map(([k, v]) => `${k}=${v}`).join(', ');
      return { pathname, params, ok: true };
    } catch (_) {
      return { pathname: url || '', params: '', ok: false };
    }
  }

  function renderConsoleEntry(e) {
    const clock = formatClock(e.ts);
    const origin = e.source
      ? `<span class="origin">${escapeHtml(e.source)}${e.line ? ':' + e.line : ''}</span>`
      : '';
    return `<li>
      <time>${escapeHtml(clock)}</time>
      <span class="level-pill level-${escapeHtml(e.level)}">${escapeHtml(e.level)}</span>
      <span class="msg">${escapeHtml(e.message || '')}</span>
      ${origin}
    </li>`;
  }

  function renderAttachment(a) {
    if (a.type === 'link') {
      const icon = KIND_ICON[a.kind] || KIND_ICON.link;
      const label = a.title || a.url;
      return `<a class="attachment-chip link" href="${escapeHtml(a.url)}" target="_blank" rel="noopener">${icon} ${escapeHtml(label)}</a>`;
    }
    if (a.type === 'image' && a.dataUrl) {
      return `<a class="attachment-chip image" href="${a.dataUrl}" target="_blank"><img src="${a.dataUrl}" alt="${escapeHtml(a.name || '')}"></a>`;
    }
    if (a.type === 'file' && a.dataUrl) {
      return `<a class="attachment-chip file" href="${a.dataUrl}" download="${escapeHtml(a.name || 'file')}">📎 ${escapeHtml(a.name || 'file')}</a>`;
    }
    return '';
  }

  function renderComment(c, i) {
    const { pathname, params, ok } = parsePageUrl(c.page_url);
    const isElement = c.region_type === 'element';
    const leftImg = c.viewportDataUrl || c.screenshotDataUrl || '';
    const cropImg = c.screenshotDataUrl || '';

    const pageLink = ok
      ? `<a href="${escapeHtml(c.page_url)}" target="_blank" rel="noopener">↗ Open page</a> <span>${escapeHtml(pathname)}</span>`
      : `<span>${escapeHtml(c.page_url || '')}</span>`;

    const metaTags = [
      `<span class="meta-tag ${isElement ? 'element' : 'region'}"><span class="label">Type:</span> ${isElement ? 'Element pin' : 'Region pin'}</span>`,
      `<span class="meta-tag"><span class="label">Page:</span> ${escapeHtml(pathname)}</span>`,
      params ? `<span class="meta-tag"><span class="label">Params:</span> ${escapeHtml(params)}</span>` : '',
      !isElement && c.bbox
        ? `<span class="meta-tag"><span class="label">Bbox:</span> x=${Math.round(c.bbox.x)}, y=${Math.round(c.bbox.y)}, w=${Math.round(c.bbox.w)}, h=${Math.round(c.bbox.h)}</span>`
        : ''
    ].filter(Boolean).join('\n      ');

    const selectorBlock = isElement && c.selector
      ? `<div class="selector-tag">${escapeHtml(c.selector)}</div>`
      : '';

    const attachments = Array.isArray(c.attachments) ? c.attachments : [];
    const attachmentsBlock = attachments.length
      ? `<div class="attachments-row">${attachments.map(renderAttachment).join('')}</div>`
      : '';

    return `<article class="comment-card">
  <header class="comment-header">
    <div class="comment-number">${i + 1}</div>
    <div class="comment-page">${pageLink}</div>
    <div class="comment-timestamp">${escapeHtml(formatTime(c.created_at))}</div>
  </header>
  <div class="comment-body">
    <div class="comment-screenshot">${leftImg ? `<img src="${leftImg}" alt="">` : ''}</div>
    <div class="comment-content">
      <div class="comment-text">${escapeHtml(c.comment || '')}</div>
      <div class="comment-meta">
      ${metaTags}
      </div>
      ${selectorBlock}
      ${attachmentsBlock}
      <div class="pin-detail">
        <div class="pin-detail-label">${isElement ? 'Pinned element' : 'Pinned region'}</div>
        ${cropImg ? `<img src="${cropImg}" alt="">` : ''}
      </div>
    </div>
  </div>
</article>`;
  }

  function buildHtmlReport({ title, reviewer, exportedAt, comments, consoleLog }) {
    const cs = Array.isArray(comments) ? comments : [];
    const logs = Array.isArray(consoleLog) ? consoleLog : [];

    const pages = new Set();
    let elementPins = 0, regionPins = 0;
    for (const c of cs) {
      try { pages.add(new URL(c.page_url).pathname); } catch (_) {}
      if (c.region_type === 'element') elementPins++; else regionPins++;
    }

    const summaryBar = `<section class="summary-bar">
  <div class="summary-stat"><div class="value">${cs.length}</div><div class="key">Total comments</div></div>
  <div class="summary-divider"></div>
  <div class="summary-stat"><div class="value">${pages.size}</div><div class="key">Pages reviewed</div></div>
  <div class="summary-divider"></div>
  <div class="summary-stat"><div class="value">${elementPins}</div><div class="key">Element pins</div></div>
  <div class="summary-divider"></div>
  <div class="summary-stat"><div class="value">${regionPins}</div><div class="key">Region pins</div></div>
</section>`;

    const consoleSection = logs.length
      ? `<section class="console-section">
  <details>
    <summary>⚠ Console errors and warnings (${logs.length})</summary>
    <ul class="console-list">
      ${logs.map(renderConsoleEntry).join('\n      ')}
    </ul>
  </details>
</section>`
      : '';

    const cards = cs.length
      ? cs.map(renderComment).join('\n')
      : `<p class="empty">No comments captured yet.</p>`;

    return `<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${escapeHtml(title)}</title>
<style>${CSS}</style>
</head>
<body>
<header class="report-header">
  <h1>${escapeHtml(title)}</h1>
  <div class="report-meta">
    <span>Reviewer: <strong>${escapeHtml(reviewer || 'anon')}</strong></span>
    <span>Generated: <strong>${escapeHtml(formatTime(exportedAt))}</strong></span>
    <span>Source: <strong>${cs.length} comment${cs.length === 1 ? '' : 's'}</strong></span>
  </div>
</header>
<main class="comments-container">
  ${summaryBar}
  ${consoleSection}
  ${cards}
</main>
</body>
</html>`;
  }

  const api = { buildHtmlReport };
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    (typeof globalThis !== 'undefined' ? globalThis : self).PFBReport = api;
  }
})();
```

- [ ] **Step 4: Run test, confirm GREEN**

```powershell
cd C:\Users\sebas\prototype-feedback-extension; node tools\test-report.js
```

Expected last stdout line: `PASS — buildHtmlReport structure verified`.

---

## Task 4: Wire `export_html` in `background.js` and the popup

**Files:**
- Modify: `C:\Users\sebas\prototype-feedback-extension\background.js`
- Modify: `C:\Users\sebas\prototype-feedback-extension\popup.html`
- Modify: `C:\Users\sebas\prototype-feedback-extension\popup.js`

- [ ] **Step 1: importScripts and add `buildHtmlReportFromStorage()` to background.js**

In `background.js`, find the line `importScripts('lib/zip.js');` and replace it with:

```js
importScripts('lib/zip.js', 'lib/report-template.js');
```

Then immediately above the existing `function llmGuide()` declaration (around line 149 — the function preceding the helpers block), add:

```js
async function buildHtmlReportFromStorage() {
  const data = await chrome.storage.local.get(STORAGE_KEY);
  const list = data[STORAGE_KEY] || [];
  const consoleData = await chrome.storage.local.get('console_log');
  const consoleLog = consoleData.console_log || [];
  const settings = await chrome.storage.local.get(
    ['reviewer', 'reviewer_email', 'project', 'active_project_id', 'active_project_name']
  );

  const projectName = settings.active_project_name || settings.project || null;
  const title = `${projectName || 'Prototype'} — Feedback Report`;

  const comments = list.map((c) => ({
    id: c.id,
    created_at: c.created_at,
    page_url: c.page_url,
    region_type: c.region_type,
    selector: c.selector,
    bbox: c.bbox,
    comment: c.comment,
    screenshotDataUrl: c.screenshot_png_b64 ? `data:image/png;base64,${c.screenshot_png_b64}` : null,
    viewportDataUrl: c.viewport_png_b64 ? `data:image/png;base64,${c.viewport_png_b64}` : null,
    attachments: (c.attachments || []).map((a) => {
      if (a.type === 'link') return { type: 'link', url: a.url, kind: a.kind, title: a.title };
      const mime = a.mime || (a.type === 'image' ? 'image/png' : 'application/octet-stream');
      return {
        type: a.type, name: a.name, mime,
        dataUrl: a.data_b64 ? `data:${mime};base64,${a.data_b64}` : null
      };
    })
  }));

  const html = PFBReport.buildHtmlReport({
    title,
    reviewer: settings.reviewer || 'anon',
    exportedAt: new Date().toISOString(),
    comments,
    consoleLog
  });

  const b64 = await stringToBase64(html);
  const url = `data:text/html;base64,${b64}`;

  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const safeReviewer = (settings.reviewer || 'anon').replace(/[^a-z0-9_-]/gi, '_');
  const filename = `prototype-feedback_${safeReviewer}_${stamp}.html`;
  await chrome.downloads.download({ url, filename, saveAs: true });
  return url;
}
```

- [ ] **Step 2: Add the `export_html` message case**

In the `switch (msg.type)` block of `background.js`, add this case alongside the existing `export` case:

```js
        case 'export_html': {
          const url = await buildHtmlReportFromStorage();
          return sendResponse({ ok: true, url });
        }
```

- [ ] **Step 3: Add the "Export HTML" button in popup.html**

Find the existing export-related button block in `popup.html`. Locate the `<button id="export">Export</button>` line. Replace it with:

```html
<button id="export">Export ZIP (for Claude)</button>
<button id="export-html">Export HTML report</button>
```

If `popup.html` uses different button text or wrapping, preserve the wrapper but add the new button immediately after the existing one. Keep the same CSS class(es) on the new button.

- [ ] **Step 4: Wire the click handler in popup.js**

In `popup.js`, find the existing handler:

```js
$('export').addEventListener('click', async () => {
  const resp = await chrome.runtime.sendMessage({ type: 'export' });
  if (!resp || !resp.ok) alert('Export failed: ' + ((resp && resp.error) || 'unknown'));
});
```

Add an analogous handler immediately after:

```js
$('export-html').addEventListener('click', async () => {
  const resp = await chrome.runtime.sendMessage({ type: 'export_html' });
  if (!resp || !resp.ok) alert('HTML export failed: ' + ((resp && resp.error) || 'unknown'));
});
```

- [ ] **Step 5: Reload extension, smoke-test export**

Reload at `chrome://extensions/`. Capture one comment on `https://example.com`. Open the popup → click "Export HTML report." A `.html` file downloads. Open it. Expected: dark header with title "Prototype — Feedback Report" (or your project name), summary bar with `1 Total comments`, one comment-card with viewport screenshot on the left.

---

## Task 5: Update `build.ps1` to include `lib/report-template.js`

**Files:**
- Modify: `C:\Users\sebas\prototype-feedback-extension\build.ps1`

- [ ] **Step 1: Add to `$required` array**

In `build.ps1`, find:

```powershell
$required = @('manifest.json', 'background.js', 'content.js', 'popup.html', 'popup.js',
              'icons\icon-16.png', 'icons\icon-48.png', 'icons\icon-128.png',
              'lib\finder.js', 'lib\zip.js', 'inject\console-tap.js')
```

Replace with:

```powershell
$required = @('manifest.json', 'background.js', 'content.js', 'popup.html', 'popup.js',
              'icons\icon-16.png', 'icons\icon-48.png', 'icons\icon-128.png',
              'lib\finder.js', 'lib\zip.js', 'lib\report-template.js',
              'inject\console-tap.js')
```

`$includes` already contains `'lib'` (recursive copy) so the new file is picked up automatically.

- [ ] **Step 2: Verify build still works**

Run:
```powershell
cd C:\Users\sebas\prototype-feedback-extension; .\build.ps1
```

Expected: `Built C:\Users\sebas\prototype-feedback-extension\dist\prototype-feedback-0.2.0.zip` (or newer version if bumped). Then verify the new file is in the ZIP:

```powershell
$tmp = "$env:TEMP\pfb-dist-task5"; if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Expand-Archive -Path "C:\Users\sebas\prototype-feedback-extension\dist\prototype-feedback-0.2.0.zip" -DestinationPath $tmp -Force
Get-ChildItem -Recurse $tmp | Where-Object { $_.Name -like 'report-template*' }
```

Expected: one match at `$tmp\lib\report-template.js`.

---

## Task 6: Server — `viewport_path` column and `ingest_payload` update

**Files:**
- Modify: `C:\Users\sebas\prototype-feedback-server\main.py`

- [ ] **Step 1: Add idempotent ALTER in `init_db`**

In `main.py`, find the `init_db` function (around line 153). After the existing `reviewer_email` block:

```python
        if not _column_exists(c, "comments", "reviewer_email"):
            c.execute("ALTER TABLE comments ADD COLUMN reviewer_email TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_comments_reviewer_email ON comments(reviewer_email)")
```

Add immediately after (still inside the `with db() as c:` block):

```python
        if not _column_exists(c, "comments", "viewport_path"):
            c.execute("ALTER TABLE comments ADD COLUMN viewport_path TEXT")
```

- [ ] **Step 2: Update `ingest_payload` to handle viewport_screenshot_path**

In `main.py`, find the screenshot-handling block inside `ingest_payload`:

```python
            # v3: screenshot_path points at a file inside source_dir
            if cm.get("screenshot_path") and source_dir is not None:
                src = (source_dir / cm["screenshot_path"]).resolve()
                ...
                screenshot_path = rel
            # v1/v2: screenshot_png_b64 inline
            elif cm.get("screenshot_png_b64"):
                ...
                screenshot_path = rel
```

Immediately after that block (still in the `for cm in comments:` loop), add the parallel viewport handling:

```python
            viewport_path = None
            if cm.get("viewport_screenshot_path") and source_dir is not None:
                src = (source_dir / cm["viewport_screenshot_path"]).resolve()
                if not str(src).startswith(str(source_dir.resolve())):
                    raise HTTPException(400, f"viewport_screenshot_path escapes archive: {cm['viewport_screenshot_path']}")
                if src.exists():
                    rel = f"batches/{batch_id}/viewport-screenshots/{cid}.png"
                    dst = DATA / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)
                    viewport_path = rel
```

- [ ] **Step 3: Insert viewport_path into the INSERT statement**

In the same loop, find the `INSERT INTO comments` statement. It currently has columns:

```python
                """INSERT INTO comments
                   (id, batch_id, created_at, edited_at, page_url, page_title,
                    region_type, selector, bbox_json, viewport_json, screenshot_path,
                    comment, reviewer, reviewer_email, project, project_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
```

Replace with:

```python
                """INSERT INTO comments
                   (id, batch_id, created_at, edited_at, page_url, page_title,
                    region_type, selector, bbox_json, viewport_json, screenshot_path,
                    viewport_path, comment, reviewer, reviewer_email, project, project_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
```

And in the VALUES tuple, insert `viewport_path,` immediately after `screenshot_path,`:

```python
                    screenshot_path,
                    viewport_path,
                    cm.get("comment"),
```

- [ ] **Step 4: Restart server, verify it starts**

Stop and restart:
```powershell
cd C:\Users\sebas\prototype-feedback-server; .\run.bat
```

Expected: `Uvicorn running on http://127.0.0.1:8090` with no migration errors. In a separate terminal:

```powershell
sqlite3 C:\Users\sebas\prototype-feedback-server\data\feedback.db "PRAGMA table_info(comments);" | findstr viewport_path
```

Expected: one line confirming `viewport_path` column exists. If `sqlite3` isn't on PATH, use Python:

```powershell
python -c "import sqlite3; c=sqlite3.connect(r'C:\Users\sebas\prototype-feedback-server\data\feedback.db'); print([r[1] for r in c.execute('PRAGMA table_info(comments)')])"
```

- [ ] **Step 5: Commit**

```powershell
cd C:\Users\sebas\prototype-feedback-server
git add main.py
git commit -m "$(cat <<'EOF'
feat(ingest): persist viewport_path for v3 viewport screenshots

Adds idempotent ALTER for comments.viewport_path and extends ingest_payload
to copy viewport-screenshots/<id>.png from a v3 ZIP into
data/batches/<batch>/viewport-screenshots/. Backward-compatible: NULL on
existing rows, HTML report falls back to the cropped screenshot.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Server — Jinja2 report template

**Files:**
- Create: `C:\Users\sebas\prototype-feedback-server\templates\report.html.j2`

- [ ] **Step 1: Create the template**

Create `C:\Users\sebas\prototype-feedback-server\templates\report.html.j2`. The CSS is identical to the extension's; the body markup uses Jinja2. Paste verbatim:

```jinja2
<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }}</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f5f7; color: #1d1d1f; }
.report-header { background: #1d1d1f; color: #f5f5f7; padding: 32px 48px; }
.report-header h1 { font-size: 28px; font-weight: 600; margin-bottom: 8px; }
.report-meta { display: flex; gap: 32px; font-size: 13px; color: #a1a1a6; margin-top: 12px; }
.report-meta span strong { color: #f5f5f7; }
.comments-container { max-width: 1100px; margin: 32px auto; padding: 0 24px; }
.comment-card { background: #fff; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 4px 16px rgba(0,0,0,0.05); margin-bottom: 28px; overflow: hidden; }
.comment-header { display: flex; align-items: center; justify-content: space-between; padding: 16px 24px; border-bottom: 1px solid #f0f0f0; background: #fafafa; }
.comment-number { width: 28px; height: 28px; border-radius: 50%; background: #0071e3; color: #fff; font-size: 13px; font-weight: 600; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.comment-page { font-size: 12px; color: #6e6e73; margin-left: 12px; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.comment-page a { color: #0071e3; text-decoration: none; }
.comment-page a:hover { text-decoration: underline; }
.comment-timestamp { font-size: 11px; color: #a1a1a6; white-space: nowrap; margin-left: 16px; }
.comment-body { display: flex; }
.comment-screenshot { flex-shrink: 0; width: 360px; background: #f5f5f7; border-right: 1px solid #f0f0f0; display: flex; align-items: flex-start; justify-content: center; padding: 16px; }
.comment-screenshot img { max-width: 100%; max-height: 320px; object-fit: contain; border-radius: 6px; box-shadow: 0 1px 4px rgba(0,0,0,0.15); }
.comment-content { flex: 1; padding: 24px; display: flex; flex-direction: column; gap: 16px; }
.comment-text { font-size: 15px; line-height: 1.6; color: #1d1d1f; white-space: pre-wrap; }
.comment-meta { display: flex; flex-wrap: wrap; gap: 8px; }
.meta-tag { display: inline-flex; align-items: center; gap: 4px; background: #f5f5f7; border: 1px solid #e5e5ea; border-radius: 6px; padding: 4px 10px; font-size: 11px; color: #6e6e73; }
.meta-tag .label { font-weight: 600; color: #1d1d1f; }
.meta-tag.region { background: #fff3e0; border-color: #ffb74d; }
.meta-tag.element { background: #e8f5e9; border-color: #81c784; }
.selector-tag { background: #f0f4ff; border: 1px solid #c5cff5; border-radius: 6px; padding: 6px 10px; font-size: 11px; font-family: "SF Mono", Menlo, monospace; color: #3451b2; word-break: break-all; }
.pin-detail { background: #f9f9fb; border: 1px solid #e5e5ea; border-radius: 8px; padding: 12px; display: flex; flex-direction: column; gap: 8px; }
.pin-detail-label { font-size: 11px; font-weight: 600; color: #6e6e73; text-transform: uppercase; letter-spacing: 0.05em; }
.pin-detail img { max-width: 100%; border-radius: 4px; border: 1px solid #e0e0e0; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
.summary-bar { background: #fff; border-radius: 12px; padding: 20px 24px; margin-bottom: 28px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); display: flex; gap: 32px; align-items: center; }
.summary-stat { text-align: center; }
.summary-stat .value { font-size: 28px; font-weight: 700; color: #0071e3; }
.summary-stat .key { font-size: 12px; color: #6e6e73; margin-top: 2px; }
.summary-divider { width: 1px; height: 40px; background: #e5e5ea; }
.console-section { background: #fff; border-radius: 12px; padding: 16px 24px; margin-bottom: 28px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.console-section summary { cursor: pointer; font-size: 14px; font-weight: 600; color: #1d1d1f; list-style: none; padding: 4px 0; }
.console-section summary::-webkit-details-marker { display: none; }
.console-list { list-style: none; padding-top: 12px; }
.console-list li { display: flex; gap: 10px; padding: 8px 0; border-top: 1px solid #f0f0f0; font-size: 12px; align-items: baseline; }
.console-list time { color: #a1a1a6; font-family: "SF Mono", Menlo, monospace; flex-shrink: 0; }
.console-list .level-pill { text-transform: uppercase; font-size: 10px; font-weight: 600; letter-spacing: 0.05em; padding: 2px 6px; border-radius: 4px; flex-shrink: 0; }
.console-list .level-error { background: #fde2e2; color: #b91c1c; }
.console-list .level-warn { background: #fef3c7; color: #92400e; }
.console-list .msg { flex: 1; color: #1d1d1f; word-break: break-word; }
.console-list .origin { font-family: "SF Mono", Menlo, monospace; color: #6e6e73; font-size: 11px; flex-shrink: 0; }
.attachments-row { display: flex; flex-wrap: wrap; gap: 8px; }
.attachment-chip { display: inline-flex; align-items: center; gap: 6px; background: #f5f5f7; border: 1px solid #e5e5ea; border-radius: 6px; padding: 6px 10px; font-size: 12px; color: #1d1d1f; text-decoration: none; }
.attachment-chip:hover { background: #ececec; }
.attachment-chip.link { background: #e8f0ff; border-color: #c5cff5; color: #3451b2; }
.attachment-chip.image img { width: 64px; height: 64px; object-fit: cover; border-radius: 4px; }
.attachment-chip.file { background: #fff8e1; border-color: #ffe0a3; }
.empty { text-align: center; padding: 48px; color: #6e6e73; font-size: 14px; }
@media (max-width: 700px) {
  .comment-body { flex-direction: column; }
  .comment-screenshot { width: 100%; border-right: none; border-bottom: 1px solid #f0f0f0; }
  .report-header { padding: 20px 16px; }
  .report-meta { flex-wrap: wrap; gap: 12px; }
}
</style>
</head>
<body>
<header class="report-header">
  <h1>{{ title }}</h1>
  <div class="report-meta">
    <span>Reviewer: <strong>{{ reviewer or 'anon' }}</strong></span>
    <span>Generated: <strong>{{ generated_at }}</strong></span>
    <span>Source: <strong>{{ comments|length }} comment{{ '' if comments|length == 1 else 's' }}</strong></span>
  </div>
</header>
<main class="comments-container">

  <section class="summary-bar">
    <div class="summary-stat"><div class="value">{{ comments|length }}</div><div class="key">Total comments</div></div>
    <div class="summary-divider"></div>
    <div class="summary-stat"><div class="value">{{ pages_reviewed }}</div><div class="key">Pages reviewed</div></div>
    <div class="summary-divider"></div>
    <div class="summary-stat"><div class="value">{{ element_pins }}</div><div class="key">Element pins</div></div>
    <div class="summary-divider"></div>
    <div class="summary-stat"><div class="value">{{ region_pins }}</div><div class="key">Region pins</div></div>
  </section>

  {% if console_log and console_log|length > 0 %}
  <section class="console-section">
    <details>
      <summary>⚠ Console errors and warnings ({{ console_log|length }})</summary>
      <ul class="console-list">
        {% for e in console_log %}
          <li>
            <time>{{ e.clock }}</time>
            <span class="level-pill level-{{ e.level }}">{{ e.level }}</span>
            <span class="msg">{{ e.message }}</span>
            {% if e.source %}<span class="origin">{{ e.source }}{% if e.line %}:{{ e.line }}{% endif %}</span>{% endif %}
          </li>
        {% endfor %}
      </ul>
    </details>
  </section>
  {% endif %}

  {% if comments|length == 0 %}
    <p class="empty">No comments captured yet.</p>
  {% endif %}

  {% for c in comments %}
    <article class="comment-card">
      <header class="comment-header">
        <div class="comment-number">{{ loop.index }}</div>
        <div class="comment-page">
          {% if c.url_ok %}
            <a href="{{ c.page_url }}" target="_blank" rel="noopener">↗ Open page</a>
            <span>{{ c.pathname }}</span>
          {% else %}
            <span>{{ c.page_url }}</span>
          {% endif %}
        </div>
        <div class="comment-timestamp">{{ c.formatted_ts }}</div>
      </header>
      <div class="comment-body">
        <div class="comment-screenshot">
          {% if c.left_image_data_url %}
            <img src="{{ c.left_image_data_url }}" alt="">
          {% endif %}
        </div>
        <div class="comment-content">
          <div class="comment-text">{{ c.comment }}</div>
          <div class="comment-meta">
            <span class="meta-tag {{ c.region_type }}"><span class="label">Type:</span>
              {{ 'Element pin' if c.region_type == 'element' else 'Region pin' }}</span>
            <span class="meta-tag"><span class="label">Page:</span> {{ c.pathname }}</span>
            {% if c.params %}
              <span class="meta-tag"><span class="label">Params:</span> {{ c.params }}</span>
            {% endif %}
            {% if c.region_type != 'element' and c.bbox %}
              <span class="meta-tag"><span class="label">Bbox:</span>
                x={{ c.bbox.x|int }}, y={{ c.bbox.y|int }}, w={{ c.bbox.w|int }}, h={{ c.bbox.h|int }}</span>
            {% endif %}
          </div>
          {% if c.region_type == 'element' and c.selector %}
            <div class="selector-tag">{{ c.selector }}</div>
          {% endif %}
          {% if c.attachments and c.attachments|length > 0 %}
            <div class="attachments-row">
              {% for a in c.attachments %}
                {% if a.type == 'link' %}
                  <a class="attachment-chip link" href="{{ a.url }}" target="_blank" rel="noopener">{{ a.icon }} {{ a.title or a.url }}</a>
                {% elif a.type == 'image' and a.data_url %}
                  <a class="attachment-chip image" href="{{ a.data_url }}" target="_blank"><img src="{{ a.data_url }}" alt="{{ a.name or '' }}"></a>
                {% elif a.type == 'file' and a.data_url %}
                  <a class="attachment-chip file" href="{{ a.data_url }}" download="{{ a.name or 'file' }}">📎 {{ a.name or 'file' }}</a>
                {% endif %}
              {% endfor %}
            </div>
          {% endif %}
          <div class="pin-detail">
            <div class="pin-detail-label">{{ 'Pinned element' if c.region_type == 'element' else 'Pinned region' }}</div>
            {% if c.crop_data_url %}
              <img src="{{ c.crop_data_url }}" alt="">
            {% endif %}
          </div>
        </div>
      </div>
    </article>
  {% endfor %}

</main>
</body>
</html>
```

- [ ] **Step 2: No verification at this step** — the template gets exercised in Task 8.

---

## Task 8: Server — `/batch/<id>/report.html` endpoint

**Files:**
- Modify: `C:\Users\sebas\prototype-feedback-server\main.py`

- [ ] **Step 1: Add report-rendering helpers near the top of `main.py`**

In `main.py`, near other helpers (just below the `SAFE_NAME = re.compile(...)` line ~170), add:

```python
KIND_ICON = {
    "google_doc": "📄", "google_sheet": "📊", "google_slide": "📑",
    "google_form": "📝", "google_docs": "📁", "google_drive": "🗂",
    "figma": "🎨", "notion": "📓", "github": "💻", "loom": "🎥",
    "miro": "🧩", "link": "🔗",
}


def _format_report_ts(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        # Cross-platform: avoid %-d / %#d by formatting parts manually
        date_part = d.strftime("%b ") + str(d.day) + d.strftime(", %Y")
        hour12 = d.hour % 12 or 12
        ampm = "AM" if d.hour < 12 else "PM"
        time_part = f"{hour12}:{d.minute:02d} {ampm}"
        return f"{date_part} · {time_part}"
    except Exception:
        return iso


def _format_report_clock(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        return iso[11:19]
    except Exception:
        return ""


def _parse_page_url(url: Optional[str]) -> tuple[str, str, bool]:
    if not url:
        return "", "", False
    try:
        from urllib.parse import urlparse, parse_qsl
        u = urlparse(url)
        pathname = u.path or "/"
        params = ", ".join(f"{k}={v}" for k, v in parse_qsl(u.query, keep_blank_values=True))
        return pathname, params, True
    except Exception:
        return url, "", False


def _file_to_data_url(rel_path: Optional[str], mime: str = "image/png") -> Optional[str]:
    if not rel_path:
        return None
    p = DATA / rel_path
    if not p.exists():
        return None
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"
```

- [ ] **Step 2: Add the endpoint**

Find the existing `batch_claude_prompt` route around line 567. Immediately after `batch_claude_json` (around line 580), add:

```python
@app.get("/batch/{batch_id}/report.html", response_class=HTMLResponse)
def batch_report_html(batch_id: str, request: Request):
    with db() as c:
        b = c.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if not b:
            raise HTTPException(404, "Batch not found")
        comment_rows = c.execute(
            "SELECT * FROM comments WHERE batch_id = ? ORDER BY created_at", (batch_id,)
        ).fetchall()
        attachments_by_comment: dict[str, list] = {}
        for cm in comment_rows:
            ats = c.execute(
                "SELECT * FROM attachments WHERE comment_id = ? ORDER BY id", (cm["id"],)
            ).fetchall()
            attachments_by_comment[cm["id"]] = [dict(a) for a in ats]

    comments: list[dict] = []
    pages = set()
    element_pins = 0
    region_pins = 0
    for cm in comment_rows:
        bbox = json.loads(cm["bbox_json"] or "{}")
        pathname, params, url_ok = _parse_page_url(cm["page_url"])
        if url_ok:
            pages.add(pathname)
        if cm["region_type"] == "element":
            element_pins += 1
        else:
            region_pins += 1

        viewport_data = _file_to_data_url(cm["viewport_path"])
        crop_data = _file_to_data_url(cm["screenshot_path"])
        left_image = viewport_data or crop_data

        ats_out: list[dict] = []
        for at in attachments_by_comment.get(cm["id"], []):
            if at.get("type") == "link":
                ats_out.append({
                    "type": "link",
                    "url": at.get("url"),
                    "title": at.get("title"),
                    "icon": KIND_ICON.get(at.get("kind"), KIND_ICON["link"]),
                })
            else:
                mime = at.get("mime") or ("image/png" if at.get("type") == "image" else "application/octet-stream")
                ats_out.append({
                    "type": at.get("type"),
                    "name": at.get("name"),
                    "data_url": _file_to_data_url(at.get("data_path"), mime),
                })

        comments.append({
            "id": cm["id"],
            "page_url": cm["page_url"],
            "pathname": pathname,
            "params": params,
            "url_ok": url_ok,
            "region_type": cm["region_type"],
            "selector": cm["selector"],
            "bbox": bbox,
            "comment": cm["comment"],
            "left_image_data_url": left_image,
            "crop_data_url": crop_data,
            "formatted_ts": _format_report_ts(cm["created_at"]),
            "attachments": ats_out,
        })

    # console_log isn't currently persisted server-side (lives only in feedback.json
    # inside the source ZIP). Look up from the original feedback.json if present.
    console_log: list[dict] = []
    src_json = BATCHES / batch_id / "feedback.json"
    if src_json.exists():
        try:
            raw = json.loads(src_json.read_text(encoding="utf-8"))
            for e in (raw.get("console_log") or []):
                console_log.append({
                    "ts": e.get("ts"),
                    "clock": _format_report_clock(e.get("ts")),
                    "level": e.get("level", "error"),
                    "message": e.get("message", ""),
                    "source": e.get("source"),
                    "line": e.get("line"),
                })
        except Exception:
            pass

    project_label = b["project"] or "Prototype"
    title = f"{project_label} — Feedback Report"

    return templates.TemplateResponse(
        "report.html.j2",
        {
            "request": request,
            "title": title,
            "reviewer": b["reviewer"],
            "generated_at": _format_report_ts(now_iso()),
            "comments": comments,
            "console_log": console_log,
            "pages_reviewed": len(pages),
            "element_pins": element_pins,
            "region_pins": region_pins,
        },
    )
```

- [ ] **Step 3: Make `feedback.json` available per batch**

The endpoint reads `console_log` from `BATCHES / batch_id / feedback.json`. The current ingest path extracts the ZIP but doesn't persist the JSON. The fix lives inside `ingest_payload` itself, where `batch_id` and `batch_dir` are already in scope.

In `main.py`'s `ingest_payload`, find the block that creates `batch_dir`:

```python
    batch_id = str(uuid.uuid4())
    batch_dir = BATCHES / batch_id
    (batch_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    (batch_dir / "attachments").mkdir(parents=True, exist_ok=True)
```

Add immediately after:

```python
    (batch_dir / "viewport-screenshots").mkdir(parents=True, exist_ok=True)
    # If we have a source_dir (v3 ZIP), persist feedback.json alongside the batch
    if source_dir is not None:
        src_json = source_dir / "feedback.json"
        if src_json.exists():
            shutil.copyfile(src_json, batch_dir / "feedback.json")
```

- [ ] **Step 4: Restart server, smoke-test endpoint with an existing batch**

Restart with `run.bat`. Pick any existing batch:

```powershell
$db = "C:\Users\sebas\prototype-feedback-server\data\feedback.db"
python -c "import sqlite3; c=sqlite3.connect(r'$db'); print(c.execute('SELECT id FROM batches LIMIT 1').fetchone())"
```

Use the returned id:

```powershell
$bid = "<paste-the-id>"
curl.exe -i "http://127.0.0.1:8090/batch/$bid/report.html" | Select-Object -First 5
```

Expected: `HTTP/1.1 200 OK`, `content-type: text/html; charset=utf-8`. If the batch was a v2 JSON (no viewport image, no `feedback.json` on disk), the report still renders — left panel uses the cropped image and the console section is omitted.

- [ ] **Step 5: Commit**

```powershell
cd C:\Users\sebas\prototype-feedback-server
git add main.py templates/report.html.j2
git commit -m "$(cat <<'EOF'
feat(report): server-side HTML report endpoint

Adds /batch/<id>/report.html rendering via templates/report.html.j2 — same
visual structure as the extension's HTML export (Apple-styled cards, summary
bar, collapsible console log). ingest_payload now persists feedback.json
into the batch dir so the report can surface console_log.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Server — Add "Report" link in the dashboard batch row

**Files:**
- Modify: `C:\Users\sebas\prototype-feedback-server\templates\index.html`

- [ ] **Step 1: Find the existing Claude column**

In `templates/index.html`, find the row that links to `/batch/{{ batch.id }}/claude` or `/batch/{{ batch.id }}/json` inside the recent-batches table.

Run:
```powershell
Select-String -Path C:\Users\sebas\prototype-feedback-server\templates\index.html -Pattern '/batch/' -SimpleMatch
```

Expected: one or more matches showing the existing batch-link cells.

- [ ] **Step 2: Insert a new "Report" anchor**

In `templates/index.html`, in the table cell that lists batch action links (look for `<a href="/batch/{{ batch.id }}/claude` or similar), add a sibling anchor immediately before or after the existing claude/json links:

```html
<a href="/batch/{{ batch.id }}/report.html" target="_blank">📄 Report</a>
```

If the cell uses spacing (e.g., `&middot;` or commas) between links, match it.

- [ ] **Step 3: Reload dashboard, verify**

Open `http://127.0.0.1:8090/` in a browser. Expected: each batch row in the "Recent batches" table now has a "📄 Report" link. Click it — opens the rendered HTML report in a new tab.

- [ ] **Step 4: Commit**

```powershell
cd C:\Users\sebas\prototype-feedback-server
git add templates/index.html
git commit -m "$(cat <<'EOF'
feat(dashboard): expose HTML report link per batch

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: End-to-end verification

**Goal:** Exercise both generators on the real user surface.

- [ ] **Step 1: Capture a session with mixed comments**

In Chrome, open `https://example.com`, then in a separate tab open `file:///C:/Users/sebas/pfb-test.html` (the test page from the prior plan if it still exists; if not, any page with a button that throws works).

Start the toolbar:
1. Click extension icon → "Start session."
2. Click the broken button on the test page (triggers `console.error` + an uncaught exception).
3. Pin an `<h1>` or other element on `example.com` with comment "Element pin test."
4. Use the region tool on the same page, drag a box, comment "Region pin test."

Open the popup. Confirm `⚠ N logs` chip is visible if you triggered any errors.

- [ ] **Step 2: Export HTML directly from the extension**

In the popup, click "Export HTML report." A `.html` file downloads.

Open it in Chrome. Expected:
- Dark header titled `Test — Feedback Report` (or whatever project name you set; defaults to "Prototype")
- Summary bar: `2 Total comments`, `1 Pages reviewed`, `1 Element pins`, `1 Region pins`
- Collapsible "Console errors and warnings (N)" section if you triggered any
- Two comment-cards numbered 1 and 2
- Left panel: viewport screenshot showing the element/region in page context
- Right panel: comment text + meta tags (Type / Page / Params? / Bbox? if region) + selector tag (element only) + Pinned-detail box with the crop

Eyeball against `~/Downloads/auth-prototype-feedback.html` — same layout, same Apple aesthetic.

- [ ] **Step 3: Also export the ZIP and ingest it server-side**

In the popup, click "Export ZIP (for Claude)." The `.zip` downloads.

Visit `http://127.0.0.1:8090/`. Drop the new `.zip` on the dropzone. Wait for ingest success.

In the "Recent batches" table, the new batch row appears. Click "📄 Report."

Expected: server-rendered HTML report opens in a new tab. Visually identical to the extension's export. Same comment count, same console log (or omitted if none), same screenshots.

- [ ] **Step 4: Backward-compat regression check**

In the same dashboard, click "📄 Report" on an OLD batch (one from a v2 JSON upload before this work).

Expected: 200 OK, report renders. Left panel uses the cropped screenshot (no viewport image). Console section omitted (old batches have no `feedback.json` on disk). Layout intact otherwise.

- [ ] **Step 5: Final commit (extension-side has no git; just verify file presence)**

```powershell
Get-ChildItem C:\Users\sebas\prototype-feedback-extension\lib\report-template.js, `
              C:\Users\sebas\prototype-feedback-extension\tools\test-report.js, `
              C:\Users\sebas\prototype-feedback-extension\dist\prototype-feedback-0.2.0.zip
```

Expected: all three present. Server-side commits from Tasks 6, 8, 9 already landed.

---

## Self-review notes

- Spec coverage: Tasks 1–2 cover viewport capture + ZIP externalization (spec §Schema delta). Task 3 covers `lib/report-template.js` (spec §Generators — extension). Tasks 4 + 5 wire the extension UI + build. Task 6 covers server schema + ingest (spec §Schema delta — server). Task 7 covers the Jinja2 template (spec §Generators — server). Task 8 covers the server endpoint. Task 9 covers dashboard exposure. Task 10 is the E2E.
- Non-goals: nothing in this plan touches multi-reviewer aggregation, reply threads, search/filter, theme toggle, print stylesheet, lightbox, full-page stitching, or source maps — confirmed.
- Symbol consistency: `viewport_png_b64` (chrome.storage), `viewport_screenshot_path` (slim JSON inside ZIP, also new column name reuses `viewport_path`), `viewport_path` (server DB column), `viewportDataUrl` (extension template input), `left_image_data_url` / `viewport_data` (server template input). All traced.
- Verification: Task 10 exercises both generators end-to-end through Chrome + dashboard.
- Preconditions: stated in Assumptions; spec referenced for canonical CSS source.
