"""Resolve a device reference from free text (chat intake, Part A).

Given a chat message ("update my plex server"), find the ONE device the
customer means by matching device name / hostname / ip_address as
case-insensitive substrings of the message. Agent-managed devices are
preferred (the apply_updates capability lives on the agent). Ambiguity —
multiple distinct devices matching with no clear winner — returns None:
never guess wrong.

This module is worker-only (the chat intake runs in the worker's Juniper
responder), so it lives in src/worker/ and is COPYed into the worker image.
"""

from models import Device

# Identity fields shorter than this are ignored for name/hostname matching —
# a 1-2 char device name would match nearly every message and cause spurious
# (and therefore wrong) binds. IP addresses are always long enough.
_MIN_NAME_LEN = 3


def _identity_fields(device) -> list:
    """The device identity strings we match against, de-duplicated in order."""
    fields = []
    for value in (device.name, device.hostname, device.ip_address):
        v = (value or "").strip()
        if v and v not in fields:
            fields.append(v)
    return fields


def _is_agent(device) -> bool:
    """Agent-managed: the NOC_Agent channel is the update/apply transport."""
    return bool(getattr(device, "adoption_method", None) == "agent"
                or getattr(device, "agent_version", None))


def referenced_devices(db, text) -> list:
    """All CLAIMED devices whose name / hostname / ip_address appears as a
    case-insensitive substring of `text` (in query order)."""
    text = (text or "").lower()
    if not text:
        return []
    out = []
    seen = set()
    for device in db.query(Device).filter(Device.claimed.is_(True)).all():
        if device.id in seen:
            continue
        for field in _identity_fields(device):
            low = field.lower()
            if len(low) < _MIN_NAME_LEN and field != device.ip_address:
                continue  # skip tiny name/hostname tokens; IPs always match
            if low in text:
                out.append(device)
                seen.add(device.id)
                break
    return out


def resolve_device_from_text(db, text, prefer_agent: bool = True):
    """Return the single device referenced in `text`, or None.

    Resolution rules (Part A brief):
      • match device name / hostname / ip_address as case-insensitive
        substrings of the message, against CLAIMED devices;
      • exactly one match → that device;
      • multiple matches → prefer the agent-managed device when exactly one
        of the matches is agent-managed (the update capability lives there);
      • otherwise (multiple matches, none clear) → None — never guess wrong.
    """
    matches = referenced_devices(db, text)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    if prefer_agent:
        agents = [d for d in matches if _is_agent(d)]
        if len(agents) == 1:
            return agents[0]
    return None
