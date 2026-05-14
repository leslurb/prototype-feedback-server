# v3 ship: ZIP export + console capture — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship prototype-feedback v3 — extension exports a ZIP with externalized PNG/file assets and a session-wide console error/warning log; server accepts the new ZIP alongside legacy JSON.

**Non-goals:**
- DEFLATE compression in the ZIP (STORED-only)
- Zip64 support
- `console.log` / `console.info` capture
- Network request capture
- Subframe / iframe console coverage
- Server's slim-conversion code-path consolidation
- Sensitive-data redaction in the console log
- In-popup viewer for console entries
- Source-map resolution of stack traces
- Initializing the extension directory as a git repo

**Architecture:**
- Extension grows two vendored-style libs — `lib/zip.js` (hand-rolled STORED zip writer, ~80 LOC) and `inject/console-tap.js` (MAIN-world shim). Schema bumps to `prototype-feedback/v3`. Internal `chrome.storage.local` shape is unchanged; externalization happens only at export time.
- Server's `/api/ingest` detects ZIP uploads by filename suffix, unzips into `data/batches/<batch-id>/`, and feeds a v3-shaped payload through a generalized `ingest_payload` that accepts either base64 (v1/v2) or file paths (v3). `write_claude_artifacts` is unchanged — it already emits paths.

**Tech Stack:** Chrome MV3 (vanilla JS), Python 3 / FastAPI, SQLite (no schema change).

**Assumptions / Preconditions:**
- `C:\Users\sebas\prototype-feedback-extension\` exists; loaded unpacked in Chrome at extension ID `lkpciiloidfgdhlgaefcbggajlfljmbp`.
- `C:\Users\sebas\prototype-feedback-server\` is on branch `main`, working tree clean, server stopped before edits.
- `node --version` works on PATH (used for unit-test harness).
- Python 3 with the server's venv set up — `run.bat` handles activation.
- A throwaway test page that throws on click (any local HTML file with `<button onclick="undefined.foo()">click me</button>` works).
- Reviewer has `7z` or PowerShell's `Expand-Archive` available for ZIP inspection.

**Rollback:**
- Extension is not a git repo. **Task 0 takes a snapshot ZIP** as backup; restore by extracting it back over the dir.
- Server is a git repo. Revert with `git restore main.py templates/` (and `git clean -fd docs/superpowers/` to discard plan/spec artifacts if desired).

---

## Task 0: Snapshot the extension as backup

**Files:**
- Create: `C:\Users\sebas\prototype-feedback-extension.bak-2026-05-14.zip`

- [ ] **Step 1: Snapshot**

Run:
```powershell
Compress-Archive -Path "C:\Users\sebas\prototype-feedback-extension\*" -DestinationPath "C:\Users\sebas\prototype-feedback-extension.bak-2026-05-14.zip" -Force
```

Expected: file `prototype-feedback-extension.bak-2026-05-14.zip` exists in `C:\Users\sebas\`, size > 100 KB.

- [ ] **Step 2: Verify**

Run:
```powershell
Get-ChildItem "C:\Users\sebas\prototype-feedback-extension.bak-2026-05-14.zip" | Select-Object Length
```

Expected: non-zero `Length`.

---

## Task 1: Implement `lib/zip.js` (STORED zip writer)

**Files:**
- Create: `C:\Users\sebas\prototype-feedback-extension\lib\zip.js`
- Test: `C:\Users\sebas\prototype-feedback-extension\tools\test-zip.js`

- [ ] **Step 1: Write the failing test**

Create `C:\Users\sebas\prototype-feedback-extension\tools\test-zip.js`:

```js
// Loads lib/zip.js in a fake-global context, builds a zip, and verifies it round-trips
// via PowerShell's Expand-Archive.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const os = require('os');
const { execSync } = require('child_process');

const libPath = path.join(__dirname, '..', 'lib', 'zip.js');
const code = fs.readFileSync(libPath, 'utf8');

const ctx = { console };
ctx.globalThis = ctx;
ctx.self = ctx;
vm.createContext(ctx);
vm.runInContext(code, ctx);

const { buildZip } = ctx.PFBZip;
if (typeof buildZip !== 'function') {
  throw new Error('PFBZip.buildZip not exposed');
}

const entries = [
  { path: 'hello.txt', bytes: new TextEncoder().encode('hello world') },
  { path: 'sub/foo.bin', bytes: new Uint8Array([0xDE, 0xAD, 0xBE, 0xEF]) },
];
const zipBytes = buildZip(entries);

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pfb-zip-'));
const zipPath = path.join(tmpDir, 'out.zip');
fs.writeFileSync(zipPath, zipBytes);

const outDir = path.join(tmpDir, 'out');
fs.mkdirSync(outDir);

execSync(
  `powershell -NoProfile -Command "Expand-Archive -Path '${zipPath}' -DestinationPath '${outDir}' -Force"`,
  { stdio: 'inherit' }
);

const hello = fs.readFileSync(path.join(outDir, 'hello.txt'), 'utf8');
if (hello !== 'hello world') throw new Error(`hello.txt content mismatch: ${hello}`);

const bin = fs.readFileSync(path.join(outDir, 'sub', 'foo.bin'));
const expected = Buffer.from([0xDE, 0xAD, 0xBE, 0xEF]);
if (!bin.equals(expected)) throw new Error(`foo.bin content mismatch`);

console.log('PASS — zip round-trip via Expand-Archive');
```

- [ ] **Step 2: Run test, confirm RED (file missing)**

Run:
```powershell
cd C:\Users\sebas\prototype-feedback-extension; node tools\test-zip.js
```

Expected: error mentioning `lib\zip.js` does not exist (ENOENT) or `PFBZip is not defined`.

- [ ] **Step 3: Implement `lib/zip.js`**

Create `C:\Users\sebas\prototype-feedback-extension\lib\zip.js`:

```js
// STORED-only ZIP writer. No DEFLATE, no Zip64, no encryption.
// Exposes globalThis.PFBZip = { buildZip } in browser/service-worker; module.exports under Node.
(function () {
  function makeCrc32Table() {
    const t = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let j = 0; j < 8; j++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      t[i] = c >>> 0;
    }
    return t;
  }
  const TABLE = makeCrc32Table();

  function crc32(bytes) {
    let crc = 0xFFFFFFFF;
    for (let i = 0; i < bytes.length; i++) {
      crc = TABLE[(crc ^ bytes[i]) & 0xFF] ^ (crc >>> 8);
    }
    return (crc ^ 0xFFFFFFFF) >>> 0;
  }

  // entries: [{path: string, bytes: Uint8Array}] — returns Uint8Array
  function buildZip(entries) {
    const enc = new TextEncoder();
    const localParts = [];
    const centralParts = [];
    let offset = 0;

    for (const e of entries) {
      const nameBytes = enc.encode(e.path);
      const data = e.bytes;
      const crc = crc32(data);
      const size = data.length;

      const lh = new DataView(new ArrayBuffer(30));
      lh.setUint32(0, 0x04034b50, true);
      lh.setUint16(4, 20, true);
      lh.setUint16(6, 0, true);
      lh.setUint16(8, 0, true);       // method 0 = STORED
      lh.setUint16(10, 0, true);
      lh.setUint16(12, 0, true);
      lh.setUint32(14, crc, true);
      lh.setUint32(18, size, true);
      lh.setUint32(22, size, true);
      lh.setUint16(26, nameBytes.length, true);
      lh.setUint16(28, 0, true);
      localParts.push(new Uint8Array(lh.buffer));
      localParts.push(nameBytes);
      localParts.push(data);

      const cd = new DataView(new ArrayBuffer(46));
      cd.setUint32(0, 0x02014b50, true);
      cd.setUint16(4, 20, true);
      cd.setUint16(6, 20, true);
      cd.setUint16(8, 0, true);
      cd.setUint16(10, 0, true);
      cd.setUint16(12, 0, true);
      cd.setUint16(14, 0, true);
      cd.setUint32(16, crc, true);
      cd.setUint32(20, size, true);
      cd.setUint32(24, size, true);
      cd.setUint16(28, nameBytes.length, true);
      cd.setUint16(30, 0, true);
      cd.setUint16(32, 0, true);
      cd.setUint16(34, 0, true);
      cd.setUint16(36, 0, true);
      cd.setUint32(38, 0, true);
      cd.setUint32(42, offset, true);
      centralParts.push(new Uint8Array(cd.buffer));
      centralParts.push(nameBytes);

      offset += 30 + nameBytes.length + size;
    }

    const cdStart = offset;
    let cdSize = 0;
    for (const p of centralParts) cdSize += p.length;

    const eo = new DataView(new ArrayBuffer(22));
    eo.setUint32(0, 0x06054b50, true);
    eo.setUint16(4, 0, true);
    eo.setUint16(6, 0, true);
    eo.setUint16(8, entries.length, true);
    eo.setUint16(10, entries.length, true);
    eo.setUint32(12, cdSize, true);
    eo.setUint32(16, cdStart, true);
    eo.setUint16(20, 0, true);

    const allParts = localParts.concat(centralParts);
    allParts.push(new Uint8Array(eo.buffer));

    let total = 0;
    for (const p of allParts) total += p.length;
    const out = new Uint8Array(total);
    let pos = 0;
    for (const p of allParts) { out.set(p, pos); pos += p.length; }
    return out;
  }

  const api = { buildZip };
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    (typeof globalThis !== 'undefined' ? globalThis : self).PFBZip = api;
  }
})();
```

- [ ] **Step 4: Run test, confirm GREEN**

Run:
```powershell
cd C:\Users\sebas\prototype-feedback-extension; node tools\test-zip.js
```

Expected last stdout line: `PASS — zip round-trip via Expand-Archive`.

- [ ] **Step 5: Add a third entry (10 KB random bytes) to the test — confirm still GREEN**

Edit `tools/test-zip.js` — extend `entries` with:

```js
{ path: 'large.bin', bytes: new Uint8Array(10240).map((_, i) => i & 0xFF) },
```

And after extraction:

```js
const large = fs.readFileSync(path.join(outDir, 'large.bin'));
if (large.length !== 10240) throw new Error(`large.bin size mismatch: ${large.length}`);
for (let i = 0; i < 10240; i++) if (large[i] !== (i & 0xFF)) throw new Error(`large.bin byte ${i} mismatch`);
```

Run again — expected: `PASS …`.

---

## Task 2: Bump schema and rewrite `_llm_guide`

**Files:**
- Modify: `C:\Users\sebas\prototype-feedback-extension\background.js:7`
- Modify: `C:\Users\sebas\prototype-feedback-extension\background.js:149-175` (the `llmGuide()` function)

- [ ] **Step 1: Bump SCHEMA constant**

Edit `background.js` line 7:
```js
const SCHEMA = 'prototype-feedback/v3';
```

- [ ] **Step 2: Replace `llmGuide()` body**

Replace the entire `llmGuide()` function (lines 149–175) with:

```js
function llmGuide() {
  return {
    purpose:
      "Pinned feedback from a human reviewer on a webpage prototype. Each comment is anchored to a specific DOM element OR a free-form viewport region, with a cropped screenshot of the target. Console errors and warnings observed during the review session are included so you can correlate UI feedback to runtime failures.",
    how_to_apply: [
      "Iterate comments[] in order; treat each as one change request.",
      "Identify the target file from page_url (or ask the user) and locate the element via selector. Selector was valid at capture time; if the DOM has changed, fall back to nearby visible text from the screenshot.",
      "View screenshot_path with the Read tool to see exactly what the reviewer was looking at — the PNG is cropped to bbox at the reviewer's devicePixelRatio.",
      "For region_type='region' there is no selector; use bbox + the screenshot to infer the target element(s) from the page source.",
      "For attachments: type='image' or 'file' — open attachments[].path with the Read tool. type='link' — fetch url with WebFetch when reachable (Google Docs/Drive may require auth).",
      "Before proposing fixes, scan console_log for errors/warnings that fired around each comment's created_at — these are often the runtime root cause the reviewer noticed but didn't articulate.",
      "Do not silently merge conflicting comments from different reviewers. Surface conflicts and ask the user.",
      "After applying, summarize per-comment what was changed (or why it was skipped) so the reviewer can verify."
    ],
    field_reference: {
      "comments[].region_type": "'element' (selector + bbox) or 'region' (bbox only)",
      "comments[].selector": "CSS selector at capture time; null when region_type='region'",
      "comments[].bbox": "{x,y,w,h} in CSS viewport pixels, NOT device pixels",
      "comments[].viewport": "{w,h,scrollX,scrollY} of the reviewer's window in CSS px",
      "comments[].screenshot_path": "Relative path inside this archive: screenshots/<comment-id>.png. PNG cropped to bbox at the reviewer's devicePixelRatio (so it is sharper than bbox dims suggest).",
      "comments[].attachments[].type": "'image' | 'file' | 'link'",
      "comments[].attachments[].path": "Present for image/file: relative path inside this archive (attachments/<comment-id>/<safe-name>).",
      "comments[].attachments[].kind": "Present for link: 'google_doc', 'google_sheet', 'google_slide', 'google_drive', 'figma', 'notion', 'github', 'loom', 'miro', or 'link'.",
      "console_log": "Session-wide JS errors and warnings captured while the reviewer was annotating. Chronological. Each entry: {ts, level, kind, message, source?, line?, col?, stack?}. Correlate to comments by timestamp (ts vs created_at)."
    },
    suggested_first_action:
      "Print a 1-line summary per comment (reviewer, page_url, region_type, selector or bbox, comment text, attachment count). Cross-reference console_log timestamps. Then ask the user which file(s) the prototype lives in before editing."
  };
}
```

- [ ] **Step 3: Verify**

Open `background.js` in an editor; confirm `const SCHEMA = 'prototype-feedback/v3';` and the new `llmGuide()` body. No further verification at this task — the export path will exercise it in Task 3.

---

## Task 3: Rewrite `buildExportDownload()` to produce a ZIP

**Files:**
- Modify: `C:\Users\sebas\prototype-feedback-extension\background.js:121-147` (the `buildExportDownload()` function)
- Modify: `C:\Users\sebas\prototype-feedback-extension\background.js:177-191` (helper functions area — add new helpers)

- [ ] **Step 1: Import `lib/zip.js` into the service worker**

Edit `background.js` top of file — add right after the SCHEMA constant (line 7):

```js
importScripts('lib/zip.js');
```

`importScripts` is available in MV3 service workers. After this line, `globalThis.PFBZip.buildZip` is available.

- [ ] **Step 2: Add helpers near the existing `bytesToBase64` block**

Insert these helpers above `bytesToBase64` (around line 184):

```js
function base64ToBytes(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function safeFilename(s) {
  return (String(s || '').replace(/[^a-zA-Z0-9._-]+/g, '_').slice(0, 100)) || 'file';
}
```

- [ ] **Step 3: Rewrite `buildExportDownload()`**

Replace the entire function (lines 121–147) with:

```js
async function buildExportDownload() {
  const data = await chrome.storage.local.get(STORAGE_KEY);
  const list = data[STORAGE_KEY] || [];
  const consoleData = await chrome.storage.local.get('console_log');
  const consoleLog = consoleData.console_log || [];
  const settings = await chrome.storage.local.get(
    ['reviewer', 'reviewer_email', 'project', 'active_project_id', 'active_project_name']
  );

  const zipEntries = [];
  const slimComments = [];

  for (const c of list) {
    const slim = {
      id: c.id,
      created_at: c.created_at,
      edited_at: c.edited_at,
      reviewer: c.reviewer,
      reviewer_email: c.reviewer_email,
      project: c.project,
      project_id: c.project_id,
      page_url: c.page_url,
      page_title: c.page_title,
      viewport: c.viewport,
      region_type: c.region_type,
      selector: c.selector,
      bbox: c.bbox,
      comment: c.comment,
      screenshot_path: null,
      attachments: []
    };

    if (c.screenshot_png_b64) {
      const p = `screenshots/${c.id}.png`;
      zipEntries.push({ path: p, bytes: base64ToBytes(c.screenshot_png_b64) });
      slim.screenshot_path = p;
    }

    const seenNames = new Map();
    for (const a of (c.attachments || [])) {
      if (a.type === 'link') {
        slim.attachments.push({
          type: 'link',
          url: a.url,
          kind: a.kind,
          title: a.title
        });
        continue;
      }
      const baseName = safeFilename(a.name || (a.type === 'image' ? 'image.png' : 'file.bin'));
      const count = seenNames.get(baseName) || 0;
      seenNames.set(baseName, count + 1);
      const finalName = count === 0 ? baseName : `${count}-${baseName}`;
      const p = `attachments/${c.id}/${finalName}`;
      if (a.data_b64) {
        zipEntries.push({ path: p, bytes: base64ToBytes(a.data_b64) });
      }
      slim.attachments.push({
        type: a.type,
        name: a.name,
        mime: a.mime,
        size: a.size,
        path: p
      });
    }

    slimComments.push(slim);
  }

  const payload = {
    schema: SCHEMA,
    exported_at: new Date().toISOString(),
    extension_version: chrome.runtime.getManifest().version,
    reviewer: settings.reviewer || null,
    reviewer_email: settings.reviewer_email || null,
    project: settings.active_project_name || settings.project || null,
    project_id: settings.active_project_id || null,
    comment_count: slimComments.length,
    _llm_guide: llmGuide(),
    comments: slimComments
  };
  if (consoleLog.length) {
    payload.console_log_count = consoleLog.length;
    payload.console_log = consoleLog;
  }

  const jsonBytes = new TextEncoder().encode(JSON.stringify(payload, null, 2));
  zipEntries.unshift({ path: 'feedback.json', bytes: jsonBytes });

  const zipBytes = PFBZip.buildZip(zipEntries);
  const blob = new Blob([zipBytes], { type: 'application/zip' });
  const url = URL.createObjectURL(blob);

  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const safeReviewer = (settings.reviewer || 'anon').replace(/[^a-z0-9_-]/gi, '_');
  const filename = `prototype-feedback_${safeReviewer}_${stamp}.zip`;
  await chrome.downloads.download({ url, filename, saveAs: true });
  return url;
}
```

- [ ] **Step 4: Reload the unpacked extension in Chrome**

Run: open `chrome://extensions/`, click the reload icon on "Prototype Feedback."

Expected: extension status shows enabled, no error chip. If error chip appears, click "Errors" — read the message; fix syntax in `background.js`.

- [ ] **Step 5: Manual smoke test**

1. Visit any web page (e.g., `https://example.com`).
2. Click the extension icon → "Start session."
3. Pin one element, write a comment, Save.
4. Click extension icon → "Export."
5. A `.zip` file is offered as download. Save it.

Expected: filename ends in `.zip`. Unzip via `Expand-Archive -Path <file>.zip -DestinationPath out -Force`. `out/feedback.json` exists, contains `"schema": "prototype-feedback/v3"` and `screenshot_path: "screenshots/<id>.png"`. `out/screenshots/<id>.png` exists and renders as a PNG.

---

## Task 4: Implement `inject/console-tap.js` (MAIN-world shim)

**Files:**
- Create: `C:\Users\sebas\prototype-feedback-extension\inject\console-tap.js`
- Test: `C:\Users\sebas\prototype-feedback-extension\tools\test-console-tap.js`

- [ ] **Step 1: Write the failing test**

Create `C:\Users\sebas\prototype-feedback-extension\tools\test-console-tap.js`:

```js
// Loads inject/console-tap.js into a fake window/global context, exercises console.error
// and an emitted 'error' event, and asserts the postMessage payload shape.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const code = fs.readFileSync(path.join(__dirname, '..', 'inject', 'console-tap.js'), 'utf8');

const ctx = {};
const received = [];
const handlers = { error: [], unhandledrejection: [] };

ctx.window = {
  __PFB_CONSOLE_TAP_LOADED: false,
  addEventListener: (name, fn) => { handlers[name] && handlers[name].push(fn); },
  postMessage: (msg) => received.push(msg),
};
ctx.window.window = ctx.window;
ctx.globalThis = ctx;
ctx.self = ctx;
ctx.crypto = { randomUUID: () => 'test-uuid-1234' };
ctx.console = { error: () => {}, warn: () => {}, log: console.log };
ctx.window.console = ctx.console;

// Map window.* references inside the shim to the same object
const origError = ctx.console.error;
const origWarn = ctx.console.warn;

// Inject helpers so the shim's references work in the sandbox
ctx.setTimeout = setTimeout;
ctx.clearTimeout = clearTimeout;
ctx.Date = Date;
ctx.Error = Error;

vm.createContext(ctx);

// Patch `window` to also be the global so `window.addEventListener` and bare `addEventListener` both work
ctx.addEventListener = ctx.window.addEventListener;
ctx.postMessage = ctx.window.postMessage;

vm.runInContext(code, ctx);

// Now exercise console.error
ctx.console.error('hello', { a: 1, b: 2 });

// Wait long enough for the 100ms batch flush
const wait = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  await wait(200);

  // Expect: an init message (with token) and at least one batch message
  const init = received.find(m => m && m.source === '__pfb_console_init');
  if (!init || init.token !== 'test-uuid-1234') throw new Error('init message missing or token wrong');

  const batch = received.find(m => m && m.source === '__pfb_console' && m.token === 'test-uuid-1234');
  if (!batch) throw new Error('batch message missing');
  if (!Array.isArray(batch.entries) || batch.entries.length === 0) throw new Error('batch.entries empty');
  const e = batch.entries[0];
  if (e.level !== 'error') throw new Error(`level wrong: ${e.level}`);
  if (e.kind !== 'console') throw new Error(`kind wrong: ${e.kind}`);
  if (!e.message.includes('hello')) throw new Error(`message missing 'hello': ${e.message}`);
  if (!e.message.includes('"a":1')) throw new Error(`message missing serialized obj: ${e.message}`);

  // Simulate uncaught error event
  if (!handlers.error.length) throw new Error('error handler not registered');
  handlers.error[0]({ message: 'boom', filename: 'a.js', lineno: 10, colno: 5, error: new Error('boom') });

  await wait(200);

  const exc = received.find(m =>
    m && m.source === '__pfb_console' && m.entries && m.entries.some(x => x.kind === 'exception')
  );
  if (!exc) throw new Error('exception entry missing');

  console.log('PASS — console-tap shape verified');
})().catch(err => { console.error(err); process.exit(1); });
```

- [ ] **Step 2: Run test, confirm RED (file missing)**

Run:
```powershell
cd C:\Users\sebas\prototype-feedback-extension; node tools\test-console-tap.js
```

Expected: ENOENT error referencing `inject\console-tap.js`.

- [ ] **Step 3: Create the shim**

Create `C:\Users\sebas\prototype-feedback-extension\inject\console-tap.js`:

```js
// MAIN-world console capture shim for Prototype Feedback.
// Wraps console.error/warn and listens for uncaught errors / rejections.
// Forwards entries to the content script via window.postMessage with a session token.
(function () {
  const w = (typeof window !== 'undefined') ? window : globalThis;
  if (w.__PFB_CONSOLE_TAP_LOADED) return;
  w.__PFB_CONSOLE_TAP_LOADED = true;

  const TOKEN = (typeof crypto !== 'undefined' && crypto.randomUUID)
    ? crypto.randomUUID()
    : (Math.random().toString(36).slice(2) + Date.now().toString(36));

  // One-shot init so content script learns the token
  try { w.postMessage({ source: '__pfb_console_init', token: TOKEN }, '*'); } catch {}

  const QUEUE = [];
  let flushTimer = null;
  function flush() {
    flushTimer = null;
    if (!QUEUE.length) return;
    const batch = QUEUE.splice(0);
    try { w.postMessage({ source: '__pfb_console', token: TOKEN, entries: batch }, '*'); } catch {}
  }
  function emit(entry) {
    QUEUE.push(entry);
    if (flushTimer) return;
    flushTimer = setTimeout(flush, 100);
  }

  function truncate(s, n) {
    if (typeof s !== 'string') return s;
    if (s.length <= n) return s;
    return s.slice(0, n - 14) + '… [truncated]';
  }

  function serializeArg(a) {
    if (a === null || a === undefined) return String(a);
    if (a instanceof Error) return `${a.name}: ${a.message}`;
    if (typeof a === 'string') return a;
    if (typeof a !== 'object') return String(a);
    try {
      const seen = new WeakSet();
      return JSON.stringify(a, (_k, v) => {
        if (typeof v === 'object' && v !== null) {
          if (seen.has(v)) return '[Circular]';
          seen.add(v);
        }
        return v;
      });
    } catch { return String(a); }
  }

  function parseStackForOrigin(stack) {
    if (!stack) return {};
    const lines = stack.split('\n').slice(1);
    for (const line of lines) {
      if (line.includes('console-tap')) continue;
      const m = line.match(/(?:at\s+(?:.*?\s+\()?)?([^()\s]+):(\d+):(\d+)\)?$/);
      if (m) return { source: m[1], line: +m[2], col: +m[3] };
    }
    return {};
  }

  const origError = w.console.error.bind(w.console);
  const origWarn = w.console.warn.bind(w.console);

  w.console.error = function (...args) {
    try { origError.apply(null, args); } catch {}
    const stack = new Error().stack;
    const origin = parseStackForOrigin(stack);
    emit({
      ts: new Date().toISOString(),
      level: 'error',
      kind: 'console',
      message: truncate(args.map(serializeArg).join(' '), 2000),
      ...(origin.source ? origin : {}),
      stack: truncate(stack, 2000),
    });
  };
  w.console.warn = function (...args) {
    try { origWarn.apply(null, args); } catch {}
    const stack = new Error().stack;
    const origin = parseStackForOrigin(stack);
    emit({
      ts: new Date().toISOString(),
      level: 'warn',
      kind: 'console',
      message: truncate(args.map(serializeArg).join(' '), 2000),
      ...(origin.source ? origin : {}),
      stack: truncate(stack, 2000),
    });
  };

  w.addEventListener('error', (e) => {
    emit({
      ts: new Date().toISOString(),
      level: 'error',
      kind: 'exception',
      message: truncate(e.message || String(e.error || ''), 2000),
      source: e.filename,
      line: e.lineno,
      col: e.colno,
      stack: truncate(e.error && e.error.stack, 2000),
    });
  });

  w.addEventListener('unhandledrejection', (e) => {
    const reason = e.reason;
    const msg = reason instanceof Error ? `${reason.name}: ${reason.message}` : String(reason);
    emit({
      ts: new Date().toISOString(),
      level: 'error',
      kind: 'rejection',
      message: truncate(msg, 2000),
      stack: truncate(reason && reason.stack, 2000),
    });
  });
})();
```

- [ ] **Step 4: Run test, confirm GREEN**

Run:
```powershell
cd C:\Users\sebas\prototype-feedback-extension; node tools\test-console-tap.js
```

Expected last stdout line: `PASS — console-tap shape verified`.

---

## Task 5: Add `inject/console-tap.js` to manifest's `web_accessible_resources`

**Files:**
- Modify: `C:\Users\sebas\prototype-feedback-extension\manifest.json`

- [ ] **Step 1: Add the entry**

Edit `manifest.json` — append after the `icons` block, before the closing brace:

```json
,
"web_accessible_resources": [
  {
    "resources": ["inject/console-tap.js"],
    "matches": ["<all_urls>"]
  }
]
```

Resulting full file should end with:

```json
  },
  "web_accessible_resources": [
    {
      "resources": ["inject/console-tap.js"],
      "matches": ["<all_urls>"]
    }
  ]
}
```

- [ ] **Step 2: Reload extension and verify no manifest errors**

Open `chrome://extensions/`, reload "Prototype Feedback."

Expected: no error chip. If shown, click "Errors" — fix JSON syntax.

---

## Task 6: Wire console capture into `content.js`

**Files:**
- Modify: `C:\Users\sebas\prototype-feedback-extension\content.js`

- [ ] **Step 1: Inject the MAIN-world shim from `popup.js`**

Edit `popup.js` — find the existing `chrome.scripting.executeScript` block around line 170:

```js
await chrome.scripting.executeScript({
  target: { tabId: tab.id },
  files: ['lib/finder.js', 'content.js']
});
```

Add a second call immediately after, for the MAIN world:

```js
await chrome.scripting.executeScript({
  target: { tabId: tab.id },
  world: 'MAIN',
  files: ['inject/console-tap.js']
});
```

- [ ] **Step 2: Add console-log message handling to `content.js`**

Edit `content.js` — insert this block after the existing `chrome.storage.local.get(...)` call near the top of the IIFE (around line 38, just before the `// ---- Shadow DOM mount` comment):

```js
  // ---- Console capture ------------------------------------------------------
  let __pfbConsoleToken = null;
  let __pfbConsoleCount = 0;

  window.addEventListener('message', (e) => {
    const d = e && e.data;
    if (!d || typeof d !== 'object') return;
    if (d.source === '__pfb_console_init' && !__pfbConsoleToken) {
      __pfbConsoleToken = d.token;
      return;
    }
    if (d.source === '__pfb_console' && d.token === __pfbConsoleToken && Array.isArray(d.entries)) {
      __pfbConsoleCount += d.entries.length;
      chrome.runtime.sendMessage({ type: 'console_append', entries: d.entries });
      updateLogChip();
    }
  });

  function refreshLogCount() {
    chrome.runtime.sendMessage({ type: 'console_get_count' }, (resp) => {
      if (resp && resp.ok) {
        __pfbConsoleCount = resp.count;
        updateLogChip();
      }
    });
  }

  function updateLogChip() {
    const chip = root && root.querySelector && root.querySelector('#log-chip');
    if (!chip) return;
    if (__pfbConsoleCount > 0) {
      chip.style.display = '';
      chip.textContent = `⚠ ${__pfbConsoleCount} log${__pfbConsoleCount === 1 ? '' : 's'}`;
    } else {
      chip.style.display = 'none';
    }
  }
```

- [ ] **Step 3: Add the log chip to the toolbar HTML**

In `content.js`, find the `<div class="toolbar" id="toolbar">` block (around line 125–130). Replace it with:

```html
    <div class="toolbar" id="toolbar">
      <button id="btn-pin">📌 Pin element</button>
      <button id="btn-region">▭ Drag region</button>
      <span class="count" id="count">0 comments</span>
      <span class="count" id="log-chip" style="display:none" title="Console errors and warnings captured this session"></span>
      <button class="close" id="btn-hide" title="Hide toolbar">✕</button>
    </div>
```

- [ ] **Step 4: Call `refreshLogCount()` from the existing `refreshCount()` body**

In `content.js`, find:

```js
function refreshCount() {
  chrome.runtime.sendMessage({ type: 'count' }, (resp) => {
    if (resp && resp.ok) countEl.textContent = `${resp.count} comment${resp.count === 1 ? '' : 's'}`;
  });
}
refreshCount();
```

Replace with:

```js
function refreshCount() {
  chrome.runtime.sendMessage({ type: 'count' }, (resp) => {
    if (resp && resp.ok) countEl.textContent = `${resp.count} comment${resp.count === 1 ? '' : 's'}`;
  });
  refreshLogCount();
}
refreshCount();
```

- [ ] **Step 5: Reload extension; verify content script still parses**

Open `chrome://extensions/`, reload the extension. Navigate to a page (e.g., `https://example.com`), click the extension icon, click "Start session." Expect the toolbar to appear in the bottom-right corner of the page. Open devtools — no errors in the console regarding `content.js`.

---

## Task 7: Add console handlers + export integration to `background.js`

**Files:**
- Modify: `C:\Users\sebas\prototype-feedback-extension\background.js`

- [ ] **Step 1: Add new message handlers and update `clear`**

Edit `background.js` — inside the `switch (msg.type)` block (around line 12), add these cases. Place them next to the existing cases:

```js
        case 'console_append': {
          const entries = msg.entries || [];
          const data = await chrome.storage.local.get('console_log');
          const log = data.console_log || [];
          for (const e of entries) {
            log.push(e);
            if (log.length > 500) log.shift();
          }
          await chrome.storage.local.set({ console_log: log });
          return sendResponse({ ok: true });
        }
        case 'console_get': {
          const data = await chrome.storage.local.get('console_log');
          return sendResponse({ ok: true, log: data.console_log || [] });
        }
        case 'console_get_count': {
          const data = await chrome.storage.local.get('console_log');
          return sendResponse({ ok: true, count: (data.console_log || []).length });
        }
        case 'console_clear': {
          await chrome.storage.local.set({ console_log: [] });
          return sendResponse({ ok: true });
        }
```

Then replace the existing `clear` case:

```js
        case 'clear': {
          await chrome.storage.local.set({ [STORAGE_KEY]: [], console_log: [] });
          await refreshBadge();
          return sendResponse({ ok: true });
        }
```

- [ ] **Step 2: Reload extension, verify no service-worker errors**

Open `chrome://extensions/`, reload "Prototype Feedback," click "Inspect views: service worker" — confirm no errors in the worker's console.

---

## Task 8: Update popup.js Clear confirmation copy

**Files:**
- Modify: `C:\Users\sebas\prototype-feedback-extension\popup.js:186`

- [ ] **Step 1: Update confirmation text**

In `popup.js`, find:

```js
$('clear').addEventListener('click', async () => {
  if (!confirm('Delete all captured comments?')) return;
```

Replace the `confirm` line with:

```js
  if (!confirm('Delete all captured comments AND clear the console log for this session?')) return;
```

- [ ] **Step 2: Reload extension, click Clear, confirm dialog shows new copy**

Open the popup, click "Clear All." Expected: confirmation dialog reads "Delete all captured comments AND clear the console log for this session?"

---

## Task 9: Generalize `ingest_payload` to accept v3 path-based comments

**Files:**
- Modify: `C:\Users\sebas\prototype-feedback-server\main.py:186-307`

- [ ] **Step 1: Add a `source_dir` parameter and path-based handling**

Edit `main.py` — replace the `ingest_payload` function signature and body. Key changes: accept an optional `source_dir: Optional[Path] = None`; when a comment has `screenshot_path` (v3), copy from `source_dir / screenshot_path` instead of decoding base64; same for attachments. Keep v1/v2 base64 path unchanged.

Replace the existing function with:

```python
def ingest_payload(payload: dict, source_filename: str, source_dir: Optional[Path] = None) -> str:
    """Insert a batch + its comments + attachments. Returns batch_id.

    For v1/v2 payloads, screenshots/attachments come from inline base64.
    For v3 payloads, they come from files inside source_dir (the unzipped temp directory).
    """
    if not isinstance(payload, dict) or "comments" not in payload:
        raise HTTPException(400, "JSON missing 'comments' array")

    schema = payload.get("schema") or "unknown"
    if not str(schema).startswith("prototype-feedback/"):
        raise HTTPException(400, f"Unsupported schema: {schema}")

    batch_id = str(uuid.uuid4())
    batch_dir = BATCHES / batch_id
    (batch_dir / "screenshots").mkdir(parents=True, exist_ok=True)
    (batch_dir / "attachments").mkdir(parents=True, exist_ok=True)

    comments = payload.get("comments", []) or []

    with db() as c:
        c.execute(
            """INSERT INTO batches
               (id, ingested_at, schema_version, extension_version, exported_at,
                reviewer, project, source_filename, comment_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                batch_id,
                now_iso(),
                schema,
                payload.get("extension_version"),
                payload.get("exported_at"),
                payload.get("reviewer"),
                payload.get("project"),
                source_filename,
                len(comments),
            ),
        )

        for cm in comments:
            cid = cm.get("id") or str(uuid.uuid4())
            screenshot_path = None

            # v3: screenshot_path points at a file inside source_dir
            if cm.get("screenshot_path") and source_dir is not None:
                src = (source_dir / cm["screenshot_path"]).resolve()
                if not str(src).startswith(str(source_dir.resolve())):
                    raise HTTPException(400, f"screenshot_path escapes archive: {cm['screenshot_path']}")
                if src.exists():
                    rel = f"batches/{batch_id}/screenshots/{cid}.png"
                    dst = DATA / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)
                    screenshot_path = rel
            # v1/v2: screenshot_png_b64 inline
            elif cm.get("screenshot_png_b64"):
                rel = f"batches/{batch_id}/screenshots/{cid}.png"
                write_b64(cm["screenshot_png_b64"], DATA / rel)
                screenshot_path = rel

            reviewer_name = cm.get("reviewer") or payload.get("reviewer")
            reviewer_email = cm.get("reviewer_email") or payload.get("reviewer_email")
            project_label = cm.get("project") or payload.get("project")
            project_id = cm.get("project_id") or payload.get("project_id")

            if not project_id and reviewer_email:
                hits = c.execute(
                    """SELECT DISTINCT project_id FROM project_reviewers
                       WHERE email = ?""",
                    (reviewer_email,),
                ).fetchall()
                if len(hits) == 1:
                    project_id = hits[0]["project_id"]

            c.execute(
                """INSERT INTO comments
                   (id, batch_id, created_at, edited_at, page_url, page_title,
                    region_type, selector, bbox_json, viewport_json, screenshot_path,
                    comment, reviewer, reviewer_email, project, project_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cid,
                    batch_id,
                    cm.get("created_at"),
                    cm.get("edited_at"),
                    cm.get("page_url"),
                    cm.get("page_title"),
                    cm.get("region_type"),
                    cm.get("selector"),
                    json.dumps(cm.get("bbox") or {}),
                    json.dumps(cm.get("viewport") or {}),
                    screenshot_path,
                    cm.get("comment"),
                    reviewer_name,
                    reviewer_email,
                    project_label,
                    project_id,
                ),
            )

            if project_id and reviewer_email:
                c.execute(
                    """UPDATE project_reviewers
                       SET status = 'commented'
                       WHERE project_id = ? AND email = ?""",
                    (project_id, reviewer_email),
                )

            for at in cm.get("attachments") or []:
                aid = str(uuid.uuid4())
                rel: Optional[str] = None

                # v3: attachment.path points at a file inside source_dir
                if at.get("type") in ("image", "file") and at.get("path") and source_dir is not None:
                    src = (source_dir / at["path"]).resolve()
                    if not str(src).startswith(str(source_dir.resolve())):
                        raise HTTPException(400, f"attachment path escapes archive: {at['path']}")
                    if src.exists():
                        fn = f"{aid}__{safe_filename(at.get('name', 'file'))}"
                        rel = f"batches/{batch_id}/attachments/{fn}"
                        dst = DATA / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(src, dst)
                # v1/v2: data_b64 inline
                elif at.get("type") in ("image", "file") and at.get("data_b64"):
                    fn = f"{aid}__{safe_filename(at.get('name', 'file'))}"
                    rel = f"batches/{batch_id}/attachments/{fn}"
                    write_b64(at["data_b64"], DATA / rel)

                c.execute(
                    """INSERT INTO attachments
                       (id, comment_id, type, name, mime, size, data_path, url, kind, title)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        aid,
                        cid,
                        at.get("type"),
                        at.get("name"),
                        at.get("mime"),
                        at.get("size"),
                        rel,
                        at.get("url"),
                        at.get("kind"),
                        at.get("title"),
                    ),
                )

    write_claude_artifacts(batch_id)
    return batch_id
```

- [ ] **Step 2: Add `shutil` import at top of `main.py`**

Verify `import shutil` is present near the other stdlib imports at the top of `main.py`. If not, add it.

- [ ] **Step 3: Commit the server change**

Run:
```powershell
cd C:\Users\sebas\prototype-feedback-server
git add main.py
git commit -m "feat(ingest): accept v3 path-based comments and attachments

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

Expected: commit succeeds.

---

## Task 10: Make `/api/ingest` detect ZIP uploads

**Files:**
- Modify: `C:\Users\sebas\prototype-feedback-server\main.py:519-532`

- [ ] **Step 1: Replace the endpoint**

Replace the existing `api_ingest` function:

```python
@app.post("/api/ingest")
async def api_ingest(files: list[UploadFile] = File(...)):
    results = []
    for f in files:
        try:
            raw = await f.read()
            fname = f.filename or "uploaded"
            is_zip = fname.lower().endswith(".zip") or (raw[:4] == b"PK\x03\x04")
            if is_zip:
                with tempfile.TemporaryDirectory(prefix="pfb-ingest-") as tmpdir:
                    tmp = Path(tmpdir)
                    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                        # Reject path traversal in any zip entry name
                        for n in zf.namelist():
                            np = (tmp / n).resolve()
                            if not str(np).startswith(str(tmp.resolve())):
                                raise HTTPException(400, f"zip entry escapes archive: {n}")
                        zf.extractall(tmp)
                    fj = tmp / "feedback.json"
                    if not fj.exists():
                        raise HTTPException(400, "ZIP missing feedback.json at root")
                    payload = json.loads(fj.read_text(encoding="utf-8"))
                    batch_id = ingest_payload(payload, fname, source_dir=tmp)
            else:
                payload = json.loads(raw.decode("utf-8"))
                batch_id = ingest_payload(payload, fname)
            results.append({"ok": True, "batch_id": batch_id, "filename": fname})
        except HTTPException as e:
            results.append({"ok": False, "error": e.detail, "filename": f.filename})
        except Exception as e:
            results.append({"ok": False, "error": str(e), "filename": f.filename})
    return JSONResponse({"results": results})
```

- [ ] **Step 2: Add imports**

Verify `import io`, `import zipfile`, `import tempfile` are present at the top of `main.py`. If any are missing, add them.

- [ ] **Step 3: Restart the server**

Run:
```powershell
cd C:\Users\sebas\prototype-feedback-server; .\run.bat
```

Expected: server logs "Uvicorn running on http://127.0.0.1:8090".

- [ ] **Step 4: Commit**

In a second terminal:
```powershell
cd C:\Users\sebas\prototype-feedback-server
git add main.py
git commit -m "feat(ingest): detect and unzip v3 ZIP uploads

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Update dashboard file input to accept `.zip`

**Files:**
- Modify: server dashboard template (one of the files in `C:\Users\sebas\prototype-feedback-server\templates\`) — locate it by grep.

- [ ] **Step 1: Find the upload form**

Run:
```powershell
cd C:\Users\sebas\prototype-feedback-server
Select-String -Path templates\*.html -Pattern 'accept=' -SimpleMatch
```

Expected: one or more matches with `accept="application/json"` or `accept=".json"`.

- [ ] **Step 2: Add `.zip` to the accept attribute**

For each match, edit the `<input type="file" accept="..."` to:

```html
accept=".zip,.json,application/zip,application/json"
```

Also, if the input has `multiple` and a label like "JSON files," update the label text to "JSON or ZIP exports."

- [ ] **Step 3: Verify in browser**

Visit `http://127.0.0.1:8090/`. Click the file input. Expected: the OS file picker now permits both `.zip` and `.json` files.

- [ ] **Step 4: Commit**

```powershell
git add templates/
git commit -m "feat(dashboard): accept .zip uploads alongside .json

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: End-to-end verification

**Goal:** Exercise the entire flow on the real user surface — extension → ZIP file → server ingest → Claude-readable output.

- [ ] **Step 1: Prepare a test prototype page**

Create `C:\Users\sebas\pfb-test.html`:

```html
<!doctype html>
<html><body>
<h1>PFB v3 E2E test</h1>
<p>Some paragraph text the reviewer might want to pin.</p>
<button id="oops" onclick="undefined.crash()">Click to throw</button>
<button id="warner" onclick="console.warn('warner clicked', {ctx: 1})">Click to warn</button>
<button id="errorer" onclick="console.error('errorer clicked', new Error('boom'))">Click to log error</button>
</body></html>
```

Open it in Chrome as `file:///C:/Users/sebas/pfb-test.html`.

- [ ] **Step 2: Capture a session**

1. Click the extension icon, set reviewer name to "E2E Tester," "Start session."
2. Click the warner button → console.warn fires.
3. Click the errorer button → console.error fires.
4. Click the oops button → uncaught TypeError fires.
5. Open the extension toolbar in the page — verify the `⚠ 3 logs` chip appears.
6. Pin the `<h1>` element, write a comment "Make the title bigger," Save.
7. Open the extension popup → click "Export."
8. A `.zip` file downloads.

Expected: the ZIP filename ends in `.zip`. Toolbar shows `⚠ 3 logs`.

- [ ] **Step 3: Verify the ZIP shape**

Run:
```powershell
$zip = (Get-ChildItem "$env:USERPROFILE\Downloads\prototype-feedback_E2E_Tester_*.zip" | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
Expand-Archive -Path $zip -DestinationPath "$env:TEMP\pfb-e2e-out" -Force
Get-ChildItem -Recurse "$env:TEMP\pfb-e2e-out" | Select-Object FullName, Length
```

Expected: `feedback.json` at the root; `screenshots/<comment-id>.png` exists; no other files unless attachments were added.

- [ ] **Step 4: Inspect `feedback.json`**

Run:
```powershell
Get-Content "$env:TEMP\pfb-e2e-out\feedback.json" | Out-String
```

Expected:
- `"schema": "prototype-feedback/v3"`
- `comments[0].screenshot_path = "screenshots/<id>.png"`
- `comments[0]` has NO `screenshot_png_b64` field
- `console_log_count` ≥ 3
- `console_log[]` contains entries with `level: "warn"` (warner), `level: "error" kind: "console"` (errorer), `level: "error" kind: "exception"` (oops)

- [ ] **Step 5: Verify Claude's `Read` tool can open the PNG**

Run from this conversation's shell:
```
Read $env:TEMP\pfb-e2e-out\screenshots\<the-id>.png
```

Expected: image renders inline (multimodal — Claude sees it).

- [ ] **Step 6: Drop the ZIP on the server**

1. Visit `http://127.0.0.1:8090/`.
2. Use the file input to upload the ZIP from `~/Downloads/`.
3. Submit.

Expected: dashboard shows a new batch with comment count = 1, no error toast.

- [ ] **Step 7: Verify server-side artifacts**

Run:
```powershell
$batchDir = Get-ChildItem "C:\Users\sebas\prototype-feedback-server\data\batches" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Get-ChildItem -Recurse $batchDir.FullName
Get-Content "$($batchDir.FullName)\claude_input.json"
```

Expected: `claude_input.json` contains the comment with `screenshot_path: "batches/<batch-id>/screenshots/<comment-id>.png"`; the PNG exists at that absolute server path.

- [ ] **Step 8: Backward-compat regression check**

Upload one of the existing v2 JSON exports in `~/Downloads/`:
```powershell
$oldJson = "C:\Users\sebas\Downloads\prototype-feedback_Seb_2026-05-11T02-12-52-656Z.json"
# Drop $oldJson on the dashboard via the file input
```

Expected: dashboard creates a new batch, screenshots end up in the batch dir (via the v2 base64 path), no errors.

---

## Self-review notes (delete this section after first execution)

- Spec coverage: tasks 1+2+3 cover the B spec end-to-end (zip writer, schema bump, export rewrite). Tasks 4+5+6+7+8 cover the A spec (shim, manifest, content, background, popup). Tasks 9+10+11 cover the server-side ZIP ingest. Task 12 exercises the whole flow.
- Non-goals: nothing in the plan implements DEFLATE, Zip64, subframe, network capture, redaction, log viewer, or extension-as-git — confirmed.
- Symbol consistency: SCHEMA constant updated in Task 2; referenced in Task 3 export function; verified in Task 12 step 4. `__pfbConsoleToken` declared in Task 6 step 2, validated in Task 6's postMessage handler. `console_log` key consistent across Task 7 and Task 3. `screenshot_path` consistent across extension export (Task 3) and server ingest (Task 9).
- Verification: Task 12 is end-to-end through the real reviewer surface (Chrome popup → file input → dashboard).
- Preconditions: Node, PowerShell, server venv, throwaway HTML — all stated in Assumptions block.
