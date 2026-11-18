"""Strict intake validation and FHIR R4 resource builders. Synthetic data only."""
import re

ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")
SPECIALTIES = {"cardiology", "dermatology", "orthopedics"}
FIELDS = {"patient_id", "payer", "member_id", "specialty", "contact"}


def validate_intake(d):
    """Reject unknown keys, wrong types and bad characters before anything is stored."""
    if not isinstance(d, dict):
        raise ValueError("body must be an object")
    extra = set(d) - FIELDS
    if extra:
        raise ValueError("unknown field")
    for k in ("patient_id", "payer", "member_id", "specialty"):
        if not isinstance(d.get(k), str) or not ID.match(d[k]):
            raise ValueError(f"invalid {k}")
    if d["specialty"] not in SPECIALTIES:
        raise ValueError("invalid specialty")
    c = d.get("contact")
    if c is not None and (not isinstance(c, str) or not EMAIL.match(c)):
        raise ValueError("invalid contact")
    return d


def service_request(ref_id, d):
    return {
        "resourceType": "ServiceRequest",
        "id": ref_id,
        "status": "active",
        "intent": "order",
        "subject": {"reference": f"Patient/{d['patient_id']}"},
        "performerType": {"coding": [{"system": "urn:lab:specialty", "code": d["specialty"]}]},
        "insurance": [{"reference": f"Coverage/{d['payer']}-{d['member_id']}"}],
    }


def task(item_id, ref_id, kind):
    return {
        "resourceType": "Task",
        "id": item_id,
        "status": "requested",
        "intent": "order",
        "code": {"text": kind},
        "focus": {"reference": f"ServiceRequest/{ref_id}"},
    }
