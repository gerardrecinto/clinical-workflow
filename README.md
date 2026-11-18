# clinical-workflow-lab

Event-driven referral workflow in plain Python, stdlib only. A referral moves through eligibility, scheduling and notification as a saga with compensation, retries, a dead-letter state, a signed HTTP API, a tamper-evident audit log and a work-item queue for steps that have no API.

Synthetic data only. Not connected to Epic, Notable, UiPath or any real EHR, and not a medical device or a HIPAA compliance claim.

Docs and design decisions: https://gerardrecinto.github.io/clinical-workflow-lab/

## Run

```
git clone https://github.com/gerardrecinto/clinical-workflow-lab
cd clinical-workflow-lab
python3 -m venv .venv && . .venv/bin/activate
pip install pytest
PYTHONPATH=src python -m cwl.demo
PYTHONPATH=src python -m pytest -q
```

The demo runs five referrals: happy path, a portal-only payer resolved through the work-item queue, inactive coverage, a late failure that releases a reserved slot, and a transient scheduler failure that retries. Expected last lines:

```
dead letters: 0
audit chain intact: True
```

To serve the API: `CWL_SECRET=change-me PYTHONPATH=src python -m cwl.api`. Requests need `X-Timestamp` and `X-Signature` (HMAC-SHA256 of `ts.METHOD.path.body`, see `security.sign`).

## Layout

- `src/cwl/bus.py` outbox, idempotent consumers, backoff, dead letters
- `src/cwl/workflow.py` saga, compensation, work-item queue with leases
- `src/cwl/fhir.py` strict intake validation, FHIR R4 ServiceRequest and Task
- `src/cwl/security.py` request signing, PHI redaction, hash-chained audit
- `src/cwl/api.py` HTTP routes
- `infra/aws` SQS with dead-letter queue, KMS, least-privilege IAM, DLQ alarm (validated in CI only, never applied)
