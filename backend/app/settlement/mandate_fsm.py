from backend.app.models import utcnow

TRANSITIONS = {
    "CREATED": {"APPROVED", "FAILED", "EXPIRED"},
    "APPROVED": {"AUTHORIZED", "FAILED", "EXPIRED"},
    "AUTHORIZED": {"ACTIVE", "CLOSING", "VOIDED", "EXPIRED"},
    "ACTIVE": {"CAPTURING", "CLOSING", "VOIDED", "EXPIRED"},
    "CAPTURING": {"ACTIVE", "CLOSING"},
    "CLOSING": {"CLOSED", "VOIDED", "EXPIRED"},
    "VOIDED": set(),
    "EXPIRED": set(),
    "FAILED": set(),
    "CLOSED": set(),
}


def transition(mandate, target):
    if target not in TRANSITIONS.get(mandate.status, set()):
        raise ValueError(f"Illegal mandate transition: {mandate.status} to {target}")
    mandate.status = target
    mandate.last_activity_at = utcnow()
    if not TRANSITIONS[target]:
        mandate.closed_at = mandate.last_activity_at
