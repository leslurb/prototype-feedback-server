# Prototype Feedback — local server

Drag-drop ingest, browse, and per-batch Claude prompt generation for JSON files
exported by the Prototype Feedback browser extension.

Runs entirely on `localhost`. No accounts, no external calls.

## Requirements

- Python 3.10 or newer (`python --version`)
- ~30 MB free for the Python virtualenv

## Setup (Windows)

```powershell
cd C:\path\to\prototype-feedback-server
.\run.bat
```

## Setup (macOS / Linux / WSL / Git-Bash)

```bash
cd /path/to/prototype-feedback-server
./run.sh
```

(First run creates `.venv/` and installs FastAPI + Uvicorn + Jinja2; takes ~30 s.
Subsequent runs are instant. Open <http://127.0.0.1:8090>.)

## Setup as a pip-installable package

If you'd rather install it system-wide or into another existing venv:

```bash
pip install /path/to/prototype-feedback-server
prototype-feedback-server                    # starts on 127.0.0.1:8090
prototype-feedback-server --port 9000        # custom port
PFB_PORT=9000 prototype-feedback-server      # via env var
```

## Use

1. Reviewer sends you a `prototype-feedback_*.json` file.
2. Drag it onto the dashboard at <http://127.0.0.1:8090>.
3. The server splits it: SQLite row per comment, screenshots and file
   attachments extracted to `data/batches/<batch_id>/`.
4. **Per batch** you get two Claude artifacts:
   - `data/batches/<batch_id>/claude_input.json` — slim manifest, paths instead of base64
   - `data/batches/<batch_id>/claude_prompt.md` — one-line prompt to paste

5. In Claude Code (started in the prototype's repo), paste the one-liner. Claude
   reads `claude_input.json`, follows the embedded `_llm_guide.how_to_apply`,
   and uses the screenshot/attachment files alongside it.

## Browse

- `/` — dashboard, drop zone, recent batches
- `/comments` — all comments, filterable by reviewer / project / batch / search
- `/comment/{id}` — full detail with screenshot, bbox, attachments, Claude prompt

## Moving to another computer

Copy the entire folder (sans `.venv/`). On the new machine, run `run.bat` /
`run.sh` and the venv will be re-created.

To move existing data too, copy `data/` along with the rest. To start fresh,
delete `data/feedback.db` and `data/batches/`.

## Configuration via environment variables

| Variable | Default | What it does |
|---|---|---|
| `PFB_HOST` | `127.0.0.1` | Bind interface. Use `0.0.0.0` to expose to LAN (no auth, so be careful). |
| `PFB_PORT` | `8090` | Bind port. |
| `PYTHON` | `python3` (sh) / `python` (bat) | Override Python interpreter. |

## File layout

```
prototype-feedback-server/
├── main.py               FastAPI app
├── pyproject.toml
├── requirements.txt
├── run.bat               Windows launcher
├── run.sh                macOS / Linux / WSL launcher
├── README.md
├── static/
│   └── style.css
├── templates/
│   ├── layout.html
│   ├── index.html
│   ├── comments.html
│   └── comment.html
└── data/                 (created on first ingest)
    ├── feedback.db                 SQLite
    └── batches/<batch_id>/
        ├── claude_input.json
        ├── claude_prompt.md
        ├── screenshots/<comment_id>.png
        └── attachments/<aid>__<filename>
```

## Schema accepted

`prototype-feedback/v1` and `prototype-feedback/v2` (with attachments + project linkage).

## Projects + Workspace invites

You can group reviewers into **projects** and use Gmail to send invites and
reminders. Project state lives in the same SQLite DB; emails are sent through
your own Google account via OAuth.

### Quick flow

1. Open <http://127.0.0.1:8090/projects> → **+ New project** (name, description, target URL).
2. **Connect Google** (top of the page) — one-time OAuth.
3. On the project page, type a name in **Add reviewer**: autocomplete pulls from
   your Workspace directory. Free-text email entry also works.
4. Click **Send invites (drafts)** — drafts open in Gmail; review and click Send.
5. Reviewers install the extension, capture, export, reply with the JSON attached.
6. When you ingest the JSON the comments **auto-tag** to the project (matched by
   reviewer email).
7. Need follow-ups? **Remind non-responders** creates one draft per reviewer
   who was invited but hasn't sent comments back.

The extension popup shows the same project list (when the local server is
running). Picking an "Active project" pre-fills `project_id` on every comment
captured, and unlocks a **📧 Send to project owner** button that drafts an
email to you with the JSON ready to attach.

## Google Workspace setup (one-time)

1. **Create a Google Cloud project** at <https://console.cloud.google.com/>.
2. Enable the **Gmail API** and the **People API** under "APIs & Services →
   Library".
3. **OAuth consent screen** → User type: *Internal* (Workspace) or *External*
   (personal). Add scopes:
   - `https://www.googleapis.com/auth/gmail.compose`
   - `https://www.googleapis.com/auth/gmail.send`
   - `https://www.googleapis.com/auth/directory.readonly`
   - `openid`, `email`
   Add yourself as a test user if Status is "Testing".
4. **Credentials** → "Create credentials" → "OAuth client ID" → Application
   type **Desktop app** → download the JSON.
5. Save the downloaded file as **one of these locations** (the server checks
   them in order):
   - `$GOOGLE_CLIENT_SECRET_PATH` env var
   - `<server>/data/google_client_secret.json`
   - `~/.config/gws/client_secret.json`
   - `%APPDATA%/gws/client_secret.json` (Windows)
6. In the dashboard, click **Connect Google** on `/projects`. A browser tab
   opens, you authorize, and the page redirects back. Token cached at
   `data/google_token.json` (refreshed automatically).

To revoke: click **Disconnect** on `/projects`, or delete `data/google_token.json`.

### Email mode

By default, every invite/reminder/reply creates a **Gmail draft** — you open
Gmail to review and click Send. The project page also has a **Send invites NOW**
override that ships immediately without the draft step.

## Schema accepted

`prototype-feedback/v1` and `prototype-feedback/v2` (with attachments + project linkage).

## Limitations

- Single-user, no auth. Stays on `127.0.0.1` by default; change at your own risk.
- SQLite; not safe for high-concurrency ingest.
- No retention policy; batches stay until you delete them via the UI.
- Google integration is per-machine (each install you copy to needs its own OAuth dance).
