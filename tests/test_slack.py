"""
Tests for SlackDelivery (slack.py).

All HTTP calls are mocked — no real webhook requests made.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from defi_tracker.core.storage import Storage
from defi_tracker.core.types import Alert, AlertSeverity
from defi_tracker.slack import SlackDelivery

WEBHOOK = "https://hooks.slack.com/services/FAKE/WEBHOOK"

# Recent timestamp — push_undelivered silently expires alerts older than 48h,
# so a hardcoded date would make every delivery test assert against 0.
TS = int(time.time()) - 60


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "test.db")
    s.init_schema()
    return s


def _write_alert(storage: Storage, kind: str = "OUT_OF_RANGE", idx: int = 0) -> int:
    alert = Alert(
        position_uid=f"0xwallet:bsc:pancake_infinity:pool:{idx}",
        kind=kind,
        severity=AlertSeverity.HIGH,
        message="Test alert message",
        triggered_at=TS + idx,
    )
    return storage.write_alert(alert)


# ── Tests ─────────────────────────────────────────────────────────────────


def test_no_alerts_returns_zero_no_http(storage: Storage):
    """With no undelivered alerts, returns 0 and makes no HTTP calls."""
    delivery = SlackDelivery(WEBHOOK)
    with patch("requests.post") as mock_post:
        result = delivery.push_undelivered(storage)
    assert result == 0
    mock_post.assert_not_called()


def test_delivers_all_alerts_and_marks_delivered(storage: Storage):
    """N alerts → posts N payloads, marks all delivered, returns N."""
    _write_alert(storage, idx=0)
    _write_alert(storage, idx=1)
    _write_alert(storage, idx=2)

    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.raise_for_status.return_value = None

    with patch("requests.post", return_value=ok_resp) as mock_post:
        result = SlackDelivery(WEBHOOK).push_undelivered(storage)

    assert result == 3
    assert mock_post.call_count == 3
    # All alerts now have delivered_at set
    assert storage.undelivered_alerts() == []


def test_http_failure_leaves_alert_undelivered(storage: Storage):
    """If the webhook POST fails, the alert stays undelivered and no exception is raised."""
    _write_alert(storage, idx=0)

    with patch("requests.post", side_effect=Exception("connection error")):
        result = SlackDelivery(WEBHOOK).push_undelivered(storage)

    assert result == 0
    # Alert is still undelivered
    assert len(storage.undelivered_alerts()) == 1


def test_partial_failure_delivers_successful_alerts(storage: Storage):
    """If one alert fails, the others still get delivered."""
    _write_alert(storage, idx=0)
    _write_alert(storage, idx=1)

    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.raise_for_status.return_value = None

    responses = [Exception("fail"), ok_resp]
    with patch("requests.post", side_effect=responses):
        result = SlackDelivery(WEBHOOK).push_undelivered(storage)

    assert result == 1
    assert len(storage.undelivered_alerts()) == 1


def test_payload_contains_position_uid_and_message(storage: Storage):
    """Delivered payload text contains position UID and alert message."""
    _write_alert(storage, kind="IL_THRESHOLD", idx=0)

    ok_resp = MagicMock()
    ok_resp.raise_for_status.return_value = None

    with patch("requests.post", return_value=ok_resp) as mock_post:
        SlackDelivery(WEBHOOK).push_undelivered(storage)

    payload = mock_post.call_args.kwargs["json"]
    assert "IL_THRESHOLD" in payload["text"]
    assert "Test alert message" in payload["text"]
