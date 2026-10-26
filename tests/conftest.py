import base64
import os
import pathlib
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.update(
    PAYPAL_CLIENT_ID="test-client", PAYPAL_SECRET="test-secret", RESET_KEY="test-reset-key",
    SPECPAY_DB=os.path.join(tempfile.mkdtemp(), "import.db"),
)
os.environ.pop("PAYPAL_WEBHOOK_ID", None)

import db  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """An empty database in a temporary file, so tests never touch your real specpay.db."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    return db


@pytest.fixture
def b64():
    return lambda data: base64.b64encode(data if isinstance(data, bytes) else data.encode()).decode()


@pytest.fixture
def sample_dxf():
    return (ROOT / "samples" / "relay_bracket_flat_pattern.dxf").read_text()


@pytest.fixture
def wrong_dxf():
    return (ROOT / "samples" / "relay_bracket_flat_pattern_WRONG.dxf").read_text()
