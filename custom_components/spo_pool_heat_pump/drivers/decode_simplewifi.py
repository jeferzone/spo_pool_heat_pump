"""Apply a byte-addressed Simple-WiFi profile map to HeatPumpState.

Unlike drivers/decode.py::apply_map (Modbus register dicts, addressed by an
int register number), this link has no register space at all: every value
lives at a byte offset inside one named (type/page) frame — or, on page 0,
inside one named group within that page (see SimpleWifiDriver's own cache).
"""

from __future__ import annotations

from typing import Any

from ..profiles import decode_outputs, decode_value, profile_registers
from .base import HeatPumpState

# The device is available as soon as both halves of page 1 have been seen
# once: power/setpoint (80/1) and temperatures/outputs/faults (D0/1) — the
# core fields a climate entity needs. compressor/fan (D0/1 byte 6) are
# already part of this and read as real values the moment it is available.
# The page-0 groups (fan_alt from 0x4F, alarm from 0x53) are diagnostics on
# their own slower, rotating schedule: until each one's own group has
# arrived, its field simply reads None/unknown — that must never hold back
# the rest of the device, and must never show as a guessed off/false either.
REQUIRED_PAGES = frozenset({(0x80, 1), (0xD0, 1)})


def _frame_key(frame: str) -> tuple[int, int]:
    type_hex, _, page_s = frame.partition("/")
    return int(type_hex, 16), int(page_s)


def _body_for(
    spec: dict[str, Any],
    pages: dict[tuple[int, int], bytes],
    groups: dict[tuple[int, int], bytes],
) -> bytes | None:
    frame = spec.get("frame")
    if not frame:
        return None
    typ, page = _frame_key(frame)
    if page == 0 and "group" in spec:
        return groups.get((typ, int(spec["group"], 16)))
    return pages.get((typ, page))


def apply_simplewifi_map(
    profile: dict[str, Any],
    pages: dict[tuple[int, int], bytes],
    groups: dict[tuple[int, int], bytes] | None = None,
) -> HeatPumpState:
    groups = groups or {}
    enums = profile.get("enums", {})
    ident = profile["identity"]
    ready = REQUIRED_PAGES <= pages.keys()
    state = HeatPumpState(
        available=ready,
        manufacturer=str(ident.get("brand") or ""),
        model=str(ident.get("model") or ""),
    )
    state.values = state.extras
    # This protocol has no mode byte: the profile commits to a single mode
    # (modes: ["heat"]), so there is nothing to decode here — just assert it,
    # which is what lets climate.py's generic hvac_action (compressor bit +
    # state.mode == "heat") work unchanged for a heat-only pump.
    state.mode = (profile.get("modes") or ["heat"])[0]

    for key, spec in profile_registers(profile).items():
        body = _body_for(spec, pages, groups)
        if body is None:
            continue
        typ = spec.get("type")
        if typ == "ascii":
            offsets = spec.get("bytes") or []
            raw_bytes = bytes(body[i] for i in offsets if i < len(body))
            text = raw_bytes.replace(b"\x00", b"").decode("ascii", "replace").strip()
            if key == "faults":
                state.faults = [text] if text else []
            elif hasattr(state, key):
                setattr(state, key, text or None)
            else:
                state.extras[key] = text or None
            continue
        byte = spec.get("byte")
        if byte is None or byte >= len(body):
            continue
        if typ == "bits":
            decoded = decode_outputs(body[byte], spec.get("bits") or {})
            if hasattr(state, key):
                setattr(state, key, decoded)
            else:
                state.extras[key] = decoded
            continue
        value = decode_value(spec, body[byte], enums)
        if hasattr(state, key):
            setattr(state, key, value)
        else:
            state.extras[key] = value
    return state
