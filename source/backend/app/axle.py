"""Small, credential-safe client for Axle's next VPP event endpoint."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

EVENT_URL = "https://api.axle.energy/vpp/home-assistant/event"


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def fetch_event(api_key: str) -> dict[str, Any] | None:
    """Return Axle's current/next event without exposing the API key in errors."""
    request = urllib.request.Request(EVENT_URL, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise HTTPException(502, "Axle could not provide VPP event data")
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise HTTPException(401, "Axle rejected the API key") from exc
        raise HTTPException(502, "Axle could not provide VPP event data") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise HTTPException(502, "Axle could not be reached") from exc
    if not isinstance(payload, dict) or not payload.get("start_time"):
        return None
    try:
        start, end = _timestamp(payload["start_time"]), _timestamp(payload["end_time"])
        if end <= start:
            raise ValueError("invalid event range")
    except (KeyError, ValueError) as exc:
        raise HTTPException(502, "Axle returned an invalid VPP event") from exc
    direction = str(payload.get("import_export") or "unknown").strip().lower()
    return {"start_at": start, "end_at": end, "direction": direction if direction in {"import", "export"} else "unknown", "updated_at": _timestamp(payload.get("updated_at")) if payload.get("updated_at") else datetime.now(timezone.utc)}
