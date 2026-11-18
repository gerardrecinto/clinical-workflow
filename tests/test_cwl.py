import json
import threading
import time
import urllib.request

import pytest

from cwl import security
from cwl.api import serve
from cwl.app import App
from cwl.bus import MAX_ATTEMPTS
from cwl.fhir import validate_intake
from cwl.workflow import Services

OK = {"patient_id": "p-1", "payer": "acme-health", "member_id": "m-101", "specialty": "cardiology",
      "contact": "a@example.org"}


def mk(**kw):
    return App(b"s", services=Services(**kw))


def state(app, rid):
    return app.view(rid)["state"]


def test_happy_path():
    app = mk()
    rid, _ = app.create_referral(OK)
    app.drain()
    assert state(app, rid) == "DONE"
    assert app.view(rid)["resource"]["resourceType"] == "ServiceRequest"


def test_idempotency_key_returns_same_referral():
    app = mk()
    a, c1 = app.create_referral(OK, "k1")
    b, c2 = app.create_referral(OK, "k1")
    assert a == b and c1 and not c2


def test_inactive_coverage_compensates():
    app = mk()
    rid, _ = app.create_referral({**OK, "member_id": "m-100"})
    app.drain()
    v = app.view(rid)
    assert v["state"] == "COMPENSATED" and v["history"][-1]["reason"] == "inactive_coverage"


def test_late_failure_releases_slot():
    app = mk(slots=1)
    rid, _ = app.create_referral({**OK, "contact": None})
    app.drain()
    assert state(app, rid) == "COMPENSATED"
    assert app.svc.slots["cardiology"] == 1


def test_transient_failure_retries_then_succeeds():
    app = mk()
    app.svc.fail_next = 2
    rid, _ = app.create_referral(OK)
    app.drain()
    assert state(app, rid) == "DONE"
    assert len(app.svc.reserved) == 1


def test_persistent_failure_goes_to_dead_letter():
    app = mk()
    app.svc.fail_next = 99
    app.create_referral(OK)
    app.drain(rounds=MAX_ATTEMPTS + 2)
    assert len(app.bus.dead()) == 1


def test_duplicate_delivery_does_not_double_reserve():
    app = mk(slots=1)
    rid, _ = app.create_referral(OK)
    app.drain()
    app.db.execute("update events set status='pending',next_at=0")
    app.db.execute("delete from processed")  # simulate lost ack: redeliver everything
    app.db.commit()
    app.drain()
    assert app.svc.slots["cardiology"] == 0 and state(app, rid) == "DONE"


def test_portal_only_waits_for_work_item():
    app = mk()
    rid, _ = app.create_referral({**OK, "payer": "portal-mutual"})
    app.drain()
    assert state(app, rid) == "WAITING"
    item = app.claim(now=10)
    assert item["item"]["resourceType"] == "Task" and app.claim(now=11) is None
    app.complete(item["item"]["id"], "active", now=12)
    app.drain()
    assert state(app, rid) == "DONE"


def test_expired_lease_is_reclaimable_and_late_complete_rejected():
    app = mk()
    app.create_referral({**OK, "payer": "portal-mutual"})
    app.drain()
    first = app.claim(now=0)
    assert app.claim(now=1000)["item"]["id"] == first["item"]["id"]
    with pytest.raises(ValueError):
        app.complete(first["item"]["id"], "active", now=5000)


def test_intake_rejects_unknown_and_malformed():
    for bad in ({**OK, "ssn": "1"}, {**OK, "patient_id": "x; drop"}, {**OK, "specialty": "x"},
                {**OK, "contact": "nope"}, [], {k: v for k, v in OK.items() if k != "payer"}):
        with pytest.raises(ValueError):
            validate_intake(bad)


def test_signature_replay_window_and_tamper():
    sig = security.sign(b"s", "1000", "POST", "/referrals", b"{}")
    assert security.verify(b"s", "1000", sig, "POST", "/referrals", b"{}", now=1100)
    assert not security.verify(b"s", "1000", sig, "POST", "/referrals", b"{}", now=2000)
    assert not security.verify(b"s", "1000", sig, "POST", "/referrals", b"{ }", now=1100)
    assert not security.verify(b"s", "1000", sig, "GET", "/referrals", b"{}", now=1100)
    assert not security.verify(b"s", "x", sig, "POST", "/referrals", b"{}", now=1100)


def test_redaction():
    t = security.redact("MRN: 12345 jo@x.org 555-123-4567 123-45-6789")
    assert "12345" not in t and "@" not in t and "555" not in t and "6789" not in t


def test_audit_chain_detects_edit():
    app = mk()
    app.create_referral(OK)
    app.drain()
    assert app.audit.verify() is None
    app.db.execute("update audit set actor='mallory' where seq=1")
    assert app.audit.verify() == 1


def test_http_end_to_end():
    app = mk()
    srv, stop = serve(app, port=0, pump_every=0.05)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    def call(method, path, obj=None, sign=True, idem=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        ts = str(int(time.time()))
        h = {"Idempotency-Key": idem} if idem else {}
        if sign:
            h.update({"X-Timestamp": ts, "X-Signature": security.sign(b"s", ts, method, path, body)})
        req = urllib.request.Request(base + path, data=body or None, method=method, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        assert call("POST", "/referrals", OK, sign=False)[0] == 401
        code, out = call("POST", "/referrals", OK, idem="a")
        assert code == 202
        assert call("POST", "/referrals", {**OK, "x": 1})[0] == 400
        for _ in range(40):
            _, v = call("GET", f"/referrals/{out['id']}")
            if v["state"] == "DONE":
                break
            time.sleep(0.05)
        assert v["state"] == "DONE"
        assert call("POST", "/referrals", OK, idem="a") == (200, {"id": out["id"]})
    finally:
        stop.set()
        srv.shutdown()
