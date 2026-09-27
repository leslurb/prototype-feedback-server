import os
import tempfile
from pathlib import Path

# MUST run before anything imports main: main.init_db() runs at import time.
_TMP = Path(tempfile.mkdtemp(prefix="pfb-test-"))
os.environ["PFB_DATA_DIR"] = str(_TMP)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

TABLES = ["status_events", "attachments", "comments", "batches", "project_reviewers",
          "email_log", "projects", "runs"]


@pytest.fixture(autouse=True)
def _clean_db():
    # Never wipe a database that is not the throwaway test one.
    assert "pfb-test-" in str(main.DB_PATH), f"refusing to wipe non-test DB {main.DB_PATH}"
    with main.db() as c:
        existing = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in TABLES:
            if t in existing:
                c.execute(f"DELETE FROM {t}")
    yield


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def tmp_data_dir():
    return _TMP
