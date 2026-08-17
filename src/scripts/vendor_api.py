#!/usr/bin/env python3
"""vendor_api executor skeleton — adapter registry for vendor HTTP/RESTCONF/
NETCONF/MQTT control (device_adoption_model.md §3/§6).

This is the thin host-side executor for the `vendor_api` channel. The registry
is extensible: each concrete vendor adapter declares its vendors, its channel,
and the actions it can execute. Concrete adapters (Juniper NETCONF, Cisco
RESTCONF, HP/Aruba HTTP, ONVIF/HTTP cameras, MQTT/HTTP IoT) are FOLLOW-UP work
— this file defines the contract so they don't re-derive the model.

Contract (an adapter must implement):
  - vendors: tuple[str]           vendor name substrings it claims
  - channel: str                  always "vendor_api"
  - actions: tuple[str]           action names it can execute
  - execute(action, params) -> dict   {"ok": bool, ...}; raise on unknown action
  - transport_note: str           one line: protocol + auth + TLS requirement

Security (design §4): adapters MUST use TLS + token/cert auth; a plaintext-HTTP
adapter is never auto-recommended (it surfaces a warning instead).
"""


class VendorAdapter:
    """Base class — a device-vendor control adapter."""

    vendors = ()
    channel = "vendor_api"
    actions = ()
    transport_note = ""

    def execute(self, action: str, params: dict) -> dict:
        if action not in self.actions:
            return {"ok": False, "error": f"{self.__class__.__name__} has no action {action!r}"}
        return {"ok": False, "error": f"{self.__class__.__name__} is a skeleton — not implemented"}


class JuniperNetconfAdapter(VendorAdapter):
    vendors = ("juniper",)
    actions = ("reboot", "collect_config", "apply_config")
    transport_note = "NETCONF over SSH/TLS (or HTTPS RESTCONF where available)"


class CiscoRestconfAdapter(VendorAdapter):
    vendors = ("cisco",)
    actions = ("reboot", "collect_config", "apply_config")
    transport_note = "RESTCONF over HTTPS (token/cert auth)"


class HpArubaHttpAdapter(VendorAdapter):
    vendors = ("hp", "aruba", "hewlett-packard")
    actions = ("reboot", "collect_config")
    transport_note = "HTTPS REST API (token auth)"


class OnvifCameraAdapter(VendorAdapter):
    vendors = ("hikvision", "dahua", "onvif", "axis", "amcrest", "reolink")
    actions = ("ptz", "collect_snapshot", "set_stream_profile")
    transport_note = "ONVIF/HTTP over HTTPS where available (plaintext HTTP warns)"


class IotHttpAdapter(VendorAdapter):
    vendors = ("iot",)
    actions = ("set_state", "collect_state")
    transport_note = "HTTP/MQTT over TLS (token auth); plaintext warns"


REGISTRY = {}


def register(adapter: VendorAdapter) -> None:
    REGISTRY[adapter.__class__.__name__] = adapter


def get_adapter(vendor: str) -> "VendorAdapter | None":
    """Resolve a vendor name (case-insensitive substring) to an adapter."""
    v = (vendor or "").lower()
    for adapter in REGISTRY.values():
        if any(k in v for k in adapter.vendors):
            return adapter
    return None


def execute(vendor: str, action: str, params: dict) -> dict:
    adapter = get_adapter(vendor)
    if adapter is None:
        return {"ok": False, "error": f"no vendor_api adapter for vendor {vendor!r}"}
    return adapter.execute(action, params or {})


# Register the skeletons so the registry is populated (follow-up workers fill
# in execute()). Registered eagerly so get_adapter()/execute() have known shapes.
for _adapter in (JuniperNetconfAdapter, CiscoRestconfAdapter, HpArubaHttpAdapter,
                 OnvifCameraAdapter, IotHttpAdapter):
    register(_adapter())


if __name__ == "__main__":
    import json
    import sys
    try:
        req = json.loads(sys.stdin.read())
    except Exception:
        req = {}
    print(json.dumps(execute(req.get("vendor", ""), req.get("action", ""),
                             req.get("params", {}))))
