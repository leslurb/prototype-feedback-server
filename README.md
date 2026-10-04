# Prototype Feedback — local server

Local FastAPI server that ingests JSON exports from the [Prototype Feedback
Chrome extension](https://chromewebstore.google.com/detail/lkpciiloidfgdhlgaefcbggajlfljmbp),
organizes them into projects, and generates ready-to-paste prompts for
Claude Code (or any AI coding assistant).

Runs entirely on `localhost`. No accounts, no servers to pay for, no
analytics. Optional Google Workspace integration uses your own Google account
via OAuth (Gmail invites + People-API directory autocomplete).

## Download

Pre-built executables for Windows, macOS, and Linux are available on the
[Releases](https://github.com/leslurb/prototype-feedback-server/releases) page.
No Python installation required — download, run, and open http://127.0.0.1:8090.

For development or customization, see the setup instructions below.

## What you get

- **Drag-drop ingest** — drop the JSON file a reviewer sent you onto the
  Batches page (`/batches`). Screenshots and attachments are split out to disk; comments go
  into SQLite.
- **Per-batch Claude prompt** — every ingest emits a slim `claude_input.json`
  (paths instead of base64) + a one-line prompt you paste into Claude Code.
- **Projects** — group reviewers, send invite emails through Gmail (drafts by
  default, with explicit "Send NOW" override), auto-tag incoming comments by
  reviewer email.
- **Workspace directory autocomplete** — start typing a name when adding a
  reviewer; the People API completes from your org.

## How it fits with the extension

```
┌──────────────┐  exports  ┌──────────────┐  emails  ┌──────────────┐
│   Reviewer   │──JSON────▶│ Project owner│──invites─▶  Reviewer    │
│ (extension)  │  (manual) │ (this server)│ (Gmail)  │ (extension)  │
└──────────────┘           └──────────────┘          └──────────────┘
                                  │
                                  └─ ingests JSON, organizes by project,
                                     emits claude_input.json + prompt
```

Reviewers only need the Chrome extension. The server is only for the
project owner — the person collecting and routing the feedback.

## Quick start

### Requirements

- **Python 3.10+** (`python3 --version` on macOS/Linux, `python --version` on Windows)
- Git
- ~30 MB free for the Python virtualenv

### Install

```bash
git clone https://github.com/leslurb/prototype-feedback-server.git
cd prototype-feedback-server
```

### Run

**macOS / Linux / WSL / Git-Bash on Windows:**

```bash
chmod +x run.sh    # one-time, makes the launcher executable
./run.sh
```

**Windows (PowerShell):**

```powershell
.\run.bat
```

First run creates `.venv/` and pip-installs FastAPI + Uvicorn + Jinja2 + the
Google libs (~45s). Subsequent starts are instant. Open
<http://127.0.0.1:8090> — you should see the dashboard. The drop zone is on
Batches (`/batches`).

That's the minimum setup. The rest of the README covers the optional Google
Workspace integration and advanced configuration.

### Install as a CLI (optional)

`pyproject.toml` lets you install system-wide:

```bash
pip install .
prototype-feedback-server                # listens on 127.0.0.1:8090
prototype-feedback-server --port 9000    # custom port
PFB_PORT=9000 prototype-feedback-server  # via env var
```

## Use

1. Reviewer sends you a `prototype-feedback_*.json` file (via email,
   Slack, AirDrop — your choice).
2. Drop it onto the Batches page at <http://127.0.0.1:8090/batches>.
3. The server splits the JSON: one SQLite row per comment, screenshots and
   attachments extracted to `data/batches/<batch_id>/`.
4. **Per batch** you get two Claude artifacts:
   - `data/batches/<batch_id>/claude_input.json` — slim manifest, paths
     instead of base64
   - `data/batches/<batch_id>/claude_prompt.md` — one-line prompt to paste
5. In Claude Code (started in the prototype's repo), paste the one-liner.
   Claude reads `claude_input.json`, follows the embedded
   `_llm_guide.how_to_apply`, and uses the screenshot / attachment files
   alongside it.

## Browse

| Path | What |
|---|---|
| `/` | Dashboard — projects, rounds, what needs you, last run |
| `/batches` | Drop zone + recent batches (was `/` before the dashboard) |
| `/round/{id}` | One round: comments by page, human items, ship |
| `/new-issue` | Compose your own issue for a project |
| `/projects` | Project list + Google connection status |
| `/project/{id}` | Project detail — reviewers, comments, invite/remind buttons |
| `/comments` | All comments, filterable by reviewer / project / batch / text |
| `/comment/{id}` | Full detail with screenshot, bbox, attachments, Claude prompt |

## Google Workspace setup (optional)

Sending invite emails and querying the Workspace directory require connecting
your Google account. **One-time setup, then automatic.**

1. **Create a Google Cloud project** at <https://console.cloud.google.com/>.
2. **Enable two APIs** under "APIs & Services → Library":
   - Gmail API
   - People API
3. **OAuth consent screen** → User type:
   - **Internal** if you're on a Google Workspace domain (recommended).
   - **External** if you're on personal `@gmail.com` — add yourself as a
     "Test user" during the Testing phase.
4. **Credentials → Create credentials → OAuth client ID** → Application
   type **Desktop app** → name it whatever → Create → **Download JSON**.

   ⚠️ Critical: pick **Desktop app**, not Web application. Loopback
   redirects only work with Desktop type.

5. Save the downloaded file to one of these paths (server checks in order):
   - `$GOOGLE_CLIENT_SECRET_PATH` env var
   - `<server>/data/google_client_secret.json`
   - `~/.config/gws/client_secret.json`
   - `%APPDATA%/gws/client_secret.json` (Windows)
6. Open <http://127.0.0.1:8090/auth/google>. A browser tab opens; sign in,
   click through the "unverified app" warning (External flow only), grant
   permissions. Token cached at `data/google_token.json`, refreshed
   automatically. To revoke: click **Disconnect** on `/projects` or delete
   the token file.

### Email mode

Every invite, reminder, and per-comment reply creates a **Gmail draft** by
default — you open Gmail to review and click Send. The project page also
has a **Send invites NOW** override that ships immediately without the draft
step.

### Multi-machine setup

The same `google_client_secret.json` works on multiple machines (your laptop,
your desktop, etc). Each machine generates its own `google_token.json` on
first auth. Google enforces a 50-refresh-token cap per (user, client) — plenty
of headroom for personal use.

## Configuration

| Variable | Default | Use when |
|---|---|---|
| `PFB_HOST` | `127.0.0.1` | Want LAN access: set to `0.0.0.0`. ⚠️ No auth — add a bearer token first or front with a reverse proxy. |
| `PFB_PORT` | `8090` | Another service is using 8090. |
| `PYTHON` | `python3` (sh) / `python` (bat) | Multiple Python installs. |
| `GOOGLE_CLIENT_SECRET_PATH` | (unset) | OAuth client JSON not in a default location. |
| `PFB_EXTRA_ORIGINS` | (unset) | Comma-separated extra CORS origins, e.g. an unpacked dev extension's `chrome-extension://<id>` origin. Only the published extension is allowed by default, so without this an unpacked dev build's popup can't reach the server. |

## Schema accepted

`prototype-feedback/v1` and `prototype-feedback/v2` (with attachments +
project linkage). The export emits a self-describing `_llm_guide` block so
Claude doesn't need an external prompt prefix.

## File layout

```
prototype-feedback-server/
├── main.py                 FastAPI app + routes + DB
├── google_oauth.py         OAuth flow, token cache
├── google_gmail.py         Gmail draft/send wrapper
├── google_people.py        Directory autocomplete
├── pyproject.toml
├── requirements.txt
├── run.bat                 Windows launcher
├── run.sh                  macOS / Linux / WSL launcher
├── static/style.css
├── templates/              Jinja2 (layout + per-route pages + email bodies)
└── data/                   (created on first run, .gitignored)
    ├── feedback.db                 SQLite
    ├── google_client_secret.json   OAuth client (you supply)
    ├── google_token.json           OAuth token (auto-cached)
    └── batches/<batch_id>/
        ├── claude_input.json
        ├── claude_prompt.md
        ├── screenshots/<comment_id>.png
        └── attachments/<aid>__<filename>
```

## Limitations

- Single-user, no auth. Stays on `127.0.0.1` by default; change at your own risk.
- SQLite; not safe for high-concurrency ingest.
- No retention policy; batches stay until you delete them via the UI.
- Google integration is per-machine — each install runs its own OAuth.

## License

MIT — see [LICENSE](./LICENSE).
