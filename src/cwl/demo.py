"""Five scenarios end to end, no network, no sleeping. Run: python -m cwl.demo"""
from .app import App
from .workflow import Services

BASE = {"patient_id": "p-1", "payer": "acme-health", "member_id": "m-101", "specialty": "cardiology",
        "contact": "a@example.org"}


def run(app, label, **over):
    rid, _ = app.create_referral({**BASE, **over})
    return label, rid


def main():
    svc = Services(slots=2)
    app = App(b"demo", services=svc)
    cases = [
        run(app, "happy path"),
        run(app, "portal-only payer", payer="portal-mutual", member_id="m-202", specialty="dermatology"),
        run(app, "inactive coverage", member_id="m-100", specialty="orthopedics"),
        run(app, "no contact after slot reserved", specialty="orthopedics", member_id="m-103", contact=None),
    ]
    app.drain()
    svc.fail_next = 1
    cases.append(run(app, "transient scheduler failure", specialty="dermatology", member_id="m-104"))
    app.drain(start=500.0)
    item = app.claim(now=1000.0)
    if item:
        app.complete(item["item"]["id"], "active", now=1001.0)
    app.drain(start=2000.0)
    for label, rid in cases:
        v = app.view(rid)
        steps = ", ".join(f"{h['step']}:{h.get('reason') or h['result']}" for h in v["history"])
        print(f"{label:34} {v['state']:12} {steps}")
    print("dead letters:", len(app.bus.dead()))
    print("audit chain intact:", app.audit.verify() is None)


if __name__ == "__main__":
    main()
