from __future__ import annotations

from agent.frame import TurnFrame


SESSION_ABORT_REPLY = "session:abort_reply"


def apply_abort_reply_slot(frame: TurnFrame) -> None:
    if SESSION_ABORT_REPLY not in frame.slots:
        return
    frame.abort = True
    frame.abort_reply = str(frame.slots[SESSION_ABORT_REPLY])
    frame.abort_reason = frame.abort_reason or f"slot:{SESSION_ABORT_REPLY}"
    frame.response = frame.abort_reply


def slot_values(frame: TurnFrame, prefix: str) -> list[str]:
    return [
        str(frame.slots[key])
        for key in sorted(frame.slots)
        if key.startswith(prefix)
    ]


def slot_int(frame: TurnFrame, key: str, default: int) -> int:
    value = frame.slots.get(key, default)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def slot_enabled(frame: TurnFrame, key: str) -> bool:
    if key not in frame.slots:
        return False
    value = frame.slots[key]
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().casefold() not in {"", "0", "false", "no", "off"}
    return bool(value)
