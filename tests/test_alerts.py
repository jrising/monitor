"""Alert emails: when they're sent, how often, batching, retries, and the feed-list settings."""
import time

import pytest
from fastapi.testclient import TestClient

import notify
import server


class Outbox(list):
    failing = False

    def fail(self, on):
        self.failing = on


@pytest.fixture
def outbox(monkeypatch):
    sent = Outbox()

    def fake_send(to, subject, body):
        if sent.failing:
            raise notify.NotifyError("SMTPAuthenticationError: bad password")
        sent.append({"to": to, "subject": subject, "body": body})
    monkeypatch.setattr(notify, "send_email", fake_send)
    monkeypatch.setattr(notify, "email_configured", lambda: "SMTP test:465")
    return sent


@pytest.fixture(autouse=True)
def clean():
    with server._lock:
        server._db.execute("DELETE FROM panels WHERE id LIKE 'al-%'")
        server._db.execute("DELETE FROM alert_state")
        server._db.commit()


def configure(**kw):
    # panels: none, so only panels created here with alert=1 can alert (other tests leave red panels around)
    server.meta_set("alerts", {"email": ["james@example.org"], "panels": "none", "after": 0,
                               "recovery": True, "repeat": None, **kw})


def make(pid, status="green", **fields):
    server.upsert(pid, {"status": status, "alert": 1, "name": pid.replace("-", " ").title(), **fields})


def test_down_once_then_recovered(outbox):
    configure()
    make("al-site", "green")
    assert server.evaluate_alerts() == [] and outbox == []
    make("al-site", "red", error="HTTP 503")
    server.evaluate_alerts()
    assert len(outbox) == 1 and outbox[0]["subject"] == "[monitor] DOWN: Al Site"
    assert "HTTP 503" in outbox[0]["body"] and outbox[0]["to"] == ["james@example.org"]
    server.evaluate_alerts()
    server.evaluate_alerts()
    assert len(outbox) == 1                              # still red: no more emails
    make("al-site", "green", error=None)
    server.evaluate_alerts()
    assert len(outbox) == 2 and outbox[1]["subject"] == "[monitor] RECOVERED: Al Site"
    server.evaluate_alerts()
    assert len(outbox) == 2


def test_after_delay_ignores_blips(outbox):
    configure(after=600)
    make("al-blip", "red", error="timeout")
    t0 = time.time()
    server.evaluate_alerts(now=t0)
    make("al-blip", "green", error=None)                 # recovered before `after`: nothing sent
    server.evaluate_alerts(now=t0 + 60)
    assert outbox == []
    make("al-blip", "red", error="timeout")
    server.evaluate_alerts(now=t0 + 100)
    server.evaluate_alerts(now=t0 + 400)
    assert outbox == []
    server.evaluate_alerts(now=t0 + 701)
    assert len(outbox) == 1 and "DOWN" in outbox[0]["subject"]


def test_stale_pushed_job_alerts(outbox):
    """A job that stops reporting has no event of its own; the evaluation still catches it."""
    configure()
    make("al-cron", "green", stale_after=3600)
    server.write("UPDATE panels SET updated_at=? WHERE id='al-cron'", (time.time() - 7200,))
    server.evaluate_alerts()
    assert len(outbox) == 1 and "no update for 2h" in outbox[0]["body"]


def test_several_failures_one_email(outbox):
    configure()
    for i in range(4):
        make(f"al-many-{i}", "red", error=f"problem {i}")
    server.evaluate_alerts()
    assert len(outbox) == 1
    assert outbox[0]["subject"].startswith("[monitor] 4 down (")
    assert all(f"problem {i}" in outbox[0]["body"] for i in range(4))


def test_failed_send_is_retried(outbox):
    configure()
    make("al-retry", "red", error="down")
    outbox.fail(True)
    assert server.evaluate_alerts() == []
    assert "bad password" in server.meta_get("alert_error")["error"]
    outbox.fail(False)
    server.evaluate_alerts()
    assert len(outbox) == 1 and server.meta_get("alert_error") is None


def test_repeat_reminders(outbox):
    configure(repeat=3600)
    make("al-repeat", "red", error="down")
    t0 = time.time()
    server.evaluate_alerts(now=t0)
    server.evaluate_alerts(now=t0 + 1800)
    server.evaluate_alerts(now=t0 + 3700)
    assert [m["subject"] for m in outbox] == ["[monitor] DOWN: Al Repeat", "[monitor] STILL DOWN: Al Repeat"]


def test_which_panels(outbox):
    configure(panels="priority")
    server.upsert("al-prio", {"status": "red", "priority": 1, "error": "x"})
    server.upsert("al-plain", {"status": "red", "priority": 0, "error": "x"})
    server.upsert("al-optout", {"status": "red", "priority": 1, "alert": 0, "error": "x"})
    server.evaluate_alerts()
    body = "\n".join(m["body"] for m in outbox)
    assert "al-prio" in body and "al-plain" not in body and "al-optout" not in body


def test_yellow_and_grey_dont_alert_or_clear(outbox):
    configure()
    make("al-yellow", "red", error="x")
    server.evaluate_alerts()
    make("al-yellow", "yellow", stage="checking…")
    server.evaluate_alerts()
    make("al-yellow", "red", error="x")
    server.evaluate_alerts()
    assert len(outbox) == 1                              # red → yellow → red is one outage


def test_feed_list_alerts_section():
    panels, alerts = server.parse_feed("panels:\n  - {id: a, alert: false}\nalerts:\n  email: me@x.org\n  after: 10m\n")
    assert alerts == {"email": ["me@x.org"], "panels": "all", "after": 600, "recovery": True, "repeat": None}
    assert panels[0]["alert"] == 0
    for bad in ["alerts:\n  email: nope", "alerts:\n  email: a@b.org\n  panels: some",
                "alerts:\n  email: a@b.org\n  sms: 123", "alertz:\n  email: a@b.org",
                "panels:\n  - {id: a, alert: maybe}"]:
        with pytest.raises(server.ConfigError):
            server.parse_feed(bad)


def test_test_email_and_health(outbox):
    c = TestClient(server.app)
    c.post("/api/login", json={"password": "test-password"})
    c.headers["X-Monitor"] = "1"
    configure()
    r = c.post("/api/alerts/test")
    assert r.status_code == 200 and outbox[-1]["subject"] == "[monitor] test email"
    outbox.fail(True)
    r = c.post("/api/alerts/test")
    assert r.status_code == 502 and "bad password" in r.json()["detail"]
    server.meta_set("last_tick", time.time() - 3600)
    assert c.get("/api/health").json()["checks_running"] is False
    server.meta_set("last_tick", time.time())
    assert c.get("/api/health").json()["checks_running"] is True
