# Console error/warning capture for prototype-feedback — design

**Date:** 2026-05-14
**Scope:** prototype-feedback-extension only (server passes the log through unchanged)
**Schema:** rides on v3 (see `2026-05-14-zip-export-design.md`)

## Problem

Reviewers describe visual issues with pinned comments, but the underlying cause is often a JavaScript error already firing in the page. Today the prototype owner has to ask "what does the console say?" and the reviewer doesn't think to check. The export should carry console errors/warnings automatically so Claude (and the human owner) sees the runtime story without prompting.

## Goal

Capture every `console.error`, `console.warn`, uncaught exception, and unhandled promise rejection that fires while the reviewer's session is active. Ship them in the export as a chronological session-wide log.

Non-goals (v1): `console.log`/`console.info`, network capture (deferred), redaction, inspecting/editing the log from the popup, subframe coverage.

## Capture mechanism

A new file `inject/console-tap.js` is injected into the page's **MAIN world** via `chrome.scripting.executeScript({ world: 'MAIN', files: ['inject/console-tap.js'] })` at the same time `content.js` is injected (toolbar start).

The shim:
- Wraps native `console.error` and `console.warn` — preserves all original behavior (page sees no change in its own logs)
- Adds `window.addEventListener('error', …)` for uncaught exceptions
- Adds `window.addEventListener('unhandledrejection', …)` for unhandled promise rejections
- Forwards each event to the content script via `window.postMessage({ source: '__pfb_console', token: <session-uuid>, entry: { … } }, '*')`

The shim closure holds a session UUID; the content script's `message` listener accepts only messages carrying that token, so a hostile page cannot inject fake entries.

The shim re-entry-guards itself with a `window.__PFB_CONSOLE_TAP_LOADED` flag so duplicate `Start session` clicks don't double-wrap.

Misses anything fired before toolbar start. Documented workflow: refresh the page with the toolbar open.

## Entry shape

```json
{
  "ts": "2026-05-14T15:23:42.117Z",
  "level": "error" | "warn",
  "kind": "console" | "exception" | "rejection",
  "message": "TypeError: foo is not a function",
  "source": "https://prototype.example.com/app.js",
  "line": 42,
  "col": 17,
  "stack": "TypeError: foo is not a function\n  at handleClick (app.js:42:17)\n  …"
}
```

- `source`, `line`, `col`, `stack` are best-effort: `error` events carry them natively; `console.*` calls don't, so the shim derives `stack` from `new Error().stack` at the call site (skipping the shim's own frame), and `source`/`line`/`col` come from parsing the top non-shim frame.
- `message` for `console.error(a, b, c)`: serialize each arg with a circular-ref replacer (`JSON.stringify` with `WeakSet` guard), join with `' '`. Strings stay strings (no quoting noise). Errors render as `${name}: ${message}` plus stack.
- Per-entry size cap: 4 KB. Truncate `message` first, then `stack`, with `… [truncated]` suffix.

## Storage and aggregation

- Content script holds an in-memory **ring buffer** capped at **500 entries**, FIFO drop on overflow.
- Each new entry triggers a 250ms debounced write to `chrome.storage.local` key `console_log` (single array). Tab reload doesn't lose history.
- Multi-tab: every tab's content script appends to the same `chrome.storage.local.console_log`. Export reads it as one chronological array.
- Existing message types in `background.js#chrome.runtime.onMessage.addListener` gain new handlers:
  - `console_append` (from content script after the debounce window) — appends entries with quota check
  - `console_get` — returns full array
  - `console_clear` — empties the array
- The existing `clear` message-type handler also clears `console_log` alongside `comments`.
- Lifecycle: log accumulates across page reloads, toolbar opens, and tab switches. Cleared only via the popup's explicit "Clear" action (same button that wipes comments) — never auto-cleared on toolbar start. This matches comment-list behavior and prevents accidental loss of pre-export context.

## Toolbar UI

Existing in-page toolbar gets one new counter chip between the comment count and the close button:

```
📌 Pin element  ▭ Drag region  · 3 comments · ⚠ 12 logs  ✕
```

- Hover title: `"Console errors and warnings captured this session"`
- Click: no-op in v1 (no inspector panel). The counter is informational, not actionable.
- Updates via the existing `refreshCount()` pattern — content script polls `console_get` count alongside `count`.
- Hidden when count is 0.

## Export integration

Rides inside the v3 `feedback.json` produced by the ZIP-export design:

```json
{
  "schema": "prototype-feedback/v3",
  "exported_at": "…",
  "comment_count": 4,
  "console_log_count": 12,
  "console_log": [ /* entries in chronological order */ ],
  "comments": [ … ],
  "_llm_guide": { … }
}
```

- `_llm_guide.field_reference` adds: `"console_log: Session-wide JS errors and warnings captured while the reviewer was annotating. Correlate to comments by timestamp (ts vs created_at)."`
- `_llm_guide.how_to_apply` adds a line: `"Before proposing fixes, scan console_log for errors that fired around each comment's created_at — these are often the root cause the reviewer noticed but didn't articulate."`
- `console_log` is omitted entirely from the JSON when the array is empty (keeps small exports tidy).

## Manifest changes

- Add `inject/console-tap.js` to `web_accessible_resources` so it can be injected into arbitrary pages from `chrome.scripting`:
  ```json
  "web_accessible_resources": [
    { "resources": ["inject/console-tap.js"], "matches": ["<all_urls>"] }
  ]
  ```
- No new permissions. `scripting` and `storage` are already declared.

## Files touched

| File | Change |
|---|---|
| `inject/console-tap.js` | New: MAIN-world shim |
| `content.js` | Listen for `__pfb_console` postMessages; ring buffer; debounced storage writes; toolbar counter chip |
| `background.js` | New message handlers: `console_append`, `console_get`, `console_clear`. Existing `clear` also wipes `console_log`. Export payload includes `console_log` + `console_log_count` (only when non-empty). |
| `popup.js` | `clear` button confirmation copy updated to mention console log |
| `manifest.json` | Add `web_accessible_resources` entry |

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Hostile page calls `postMessage` impersonating `__pfb_console` | Session UUID token in shim closure; content script validates token before accepting |
| Page logs thousands of errors → buffer churn, postMessage flood | Cap at 500 FIFO; shim batches in 100 ms windows, posts arrays not single entries |
| Sensitive data in console output (PII, tokens) | Documented in README; reviewer can `clear` before export. v2: regex-based redaction. |
| Pre-toolbar errors missed | Documented workflow: refresh with toolbar open. Hint chip on first run (deferred to v2). |
| Subframes / iframes | v1: top frame only. v2: inject into subframes via `allFrames: true` (will need verification that postMessage cross-frame relay works correctly). |
| `chrome.scripting.executeScript` with `world: 'MAIN'` is MV3-only | Already MV3; verified. |
| Two `Start session` clicks → double-wrapped console | Re-entry guard via `window.__PFB_CONSOLE_TAP_LOADED` matches the existing `content.js` pattern |
| Quota exhaustion on `chrome.storage.local` from huge logs | 500-entry cap × 4 KB = 2 MB worst-case, well under the 5 MB default per-key limit |

## Testing

- **Unit (shim):** load `console-tap.js` into a Node/JSDOM harness; call `console.error("foo", {a:1})`; assert the dispatched event has level `error`, `message: 'foo {"a":1}'`, `kind: 'console'`, valid `stack`
- **Unit (content):** simulate inbound postMessages, assert ring buffer caps at 500 and drops oldest
- **Integration:** prototype page that throws on click; start toolbar; click broken element; pin a comment; export; verify the exception appears in `feedback.json#console_log` with correct ts ≈ created_at
- **Integration:** mock a 1000-warning page; verify exactly 500 entries in `console_log`, all the most recent 500
- **Integration:** two tabs both running toolbar, both logging; export from popup; verify chronological aggregation
- **Security:** from devtools, run `window.postMessage({source:'__pfb_console', entry:{...}}, '*')`; verify content script rejects it (no token)

## Out of scope (v2 candidates)

- `console.log` / `console.info` capture
- Network request capture (4xx/5xx, failed fetches)
- Subframe coverage (`allFrames: true`)
- In-popup viewer to inspect/delete individual log entries
- Regex-based redaction of sensitive strings
- "Hint chip" prompting reviewer to refresh for pre-load coverage
- Source-map resolution of `stack` line/col
