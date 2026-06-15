"""Helpers for discovering Google Chat spaces not returned by spaces.list."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_LIST_SPACES_API_PAGE_SIZE = 1000
_LIST_SPACES_MAX_PAGES = 20
_CHAT_SPACE_ID_PATTERN = re.compile(
    r"(?:chat/space/|chat\.google\.com/room/|#chat/space/)([A-Za-z0-9_-]+)"
)
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def registry_path() -> Path:
    override = os.getenv("WORKSPACE_MCP_CHAT_SPACE_REGISTRY", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return Path(__file__).resolve().parents[1] / "data" / "chat-space-registry.json"


def normalize_space_name(text: str) -> str:
    normalized = text.lower()
    normalized = re.sub(r"[–—\-]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def extract_space_id_from_url(text: str) -> Optional[str]:
    if not text:
        return None
    match = _CHAT_SPACE_ID_PATTERN.search(text.strip())
    if not match:
        bare = text.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{6,}", bare):
            return bare
        return None
    return match.group(1)


def normalize_space_resource_name(space_id: str) -> str:
    normalized = space_id.strip()
    if not normalized.startswith("spaces/"):
        normalized = f"spaces/{normalized}"
    return normalized


def parse_date_from_text(text: str, *, default_year: Optional[int] = None) -> Optional[str]:
    """Return ISO date YYYY-MM-DD parsed from strings like 'Jun 11' or '2026-06-11'."""
    if not text:
        return None
    year = default_year or datetime.utcnow().year
    iso_match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
    if iso_match:
        return f"{iso_match.group(1)}-{iso_match.group(2)}-{iso_match.group(3)}"
    month_day = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?)\s+(\d{1,2})(?:,?\s+(20\d{2}))?\b",
        text,
        flags=re.IGNORECASE,
    )
    if not month_day:
        return None
    month = _MONTHS[month_day.group(1).lower()]
    day = int(month_day.group(2))
    if month_day.group(3):
        year = int(month_day.group(3))
    return f"{year:04d}-{month:02d}-{day:02d}"


def build_display_name_candidates(name: str, event_date: Optional[str] = None) -> List[str]:
    candidates: List[str] = []
    base = name.strip()
    if base:
        candidates.append(base)

    parsed_date = event_date or parse_date_from_text(name)
    if not parsed_date:
        return list(dict.fromkeys(candidates))

    try:
        dt = datetime.strptime(parsed_date, "%Y-%m-%d")
    except ValueError:
        return list(dict.fromkeys(candidates))

    month_abbr = dt.strftime("%b")
    month_full = dt.strftime("%B")
    day = dt.day
    prefix = base
    month_day_suffix = re.search(
        r"\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?)\s+\d{1,2}(?:,?\s+20\d{2})?\s*$",
        base,
        flags=re.IGNORECASE,
    )
    if month_day_suffix:
        prefix = base[: month_day_suffix.start()].strip(" -–")
    if not prefix:
        prefix = "Daily Sync"

    candidates.extend(
        [
            f"{prefix} - {month_abbr} {day}",
            f"{prefix} – {month_abbr} {day}",
            f"{prefix} - {month_full} {day}",
            f"{prefix} – {month_full} {day}",
        ]
    )
    return list(dict.fromkeys(candidates))


def _empty_registry() -> dict:
    return {"by_name": {}, "spaces": {}, "candidate_ids": []}


def load_registry() -> dict:
    path = registry_path()
    if not path.exists():
        return _empty_registry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read chat space registry at %s: %s", path, exc)
        return _empty_registry()
    if not isinstance(data, dict):
        return _empty_registry()
    data.setdefault("by_name", {})
    data.setdefault("spaces", {})
    data.setdefault("candidate_ids", [])
    return data


def save_registry(data: dict) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def register_space(
    *,
    space_id: str,
    display_name: str,
    meet_code: Optional[str] = None,
    event_instance_id: Optional[str] = None,
) -> None:
    resource = normalize_space_resource_name(space_id)
    bare_id = resource.split("/", 1)[-1]
    data = load_registry()
    by_name = data["by_name"]
    spaces = data["spaces"]
    candidates: List[str] = list(data.get("candidate_ids", []))

    normalized = normalize_space_name(display_name)
    if normalized:
        by_name[normalized] = resource
    spaces[resource] = {
        "display_name": display_name,
        "meet_code": meet_code,
        "event_instance_id": event_instance_id,
        "discovered_at": datetime.utcnow().isoformat() + "Z",
    }
    if bare_id not in candidates:
        candidates.insert(0, bare_id)
    data["candidate_ids"] = candidates[:500]
    save_registry(data)


def lookup_registry_by_name(name: str) -> Optional[str]:
    data = load_registry()
    return data.get("by_name", {}).get(normalize_space_name(name))


def lookup_registry_by_event(event_instance_id: str) -> Optional[str]:
    if not event_instance_id:
        return None
    data = load_registry()
    for resource, meta in data.get("spaces", {}).items():
        if meta.get("event_instance_id") == event_instance_id:
            return resource
    return None


def add_candidate_ids(space_ids: Set[str]) -> None:
    if not space_ids:
        return
    data = load_registry()
    candidates: List[str] = list(data.get("candidate_ids", []))
    for space_id in space_ids:
        bare = space_id.replace("spaces/", "")
        if bare and bare not in candidates:
            candidates.insert(0, bare)
    data["candidate_ids"] = candidates[:500]
    save_registry(data)


def filter_spaces_by_display_name(spaces: List[dict], display_name: str) -> List[dict]:
    query = display_name.strip()
    if not query:
        return spaces
    if " " not in normalize_space_name(query):
        needle = normalize_space_name(query)
        return [
            space
            for space in spaces
            if needle in normalize_space_name(space.get("displayName") or "")
        ]
    tokens = [token for token in normalize_space_name(query).split() if token]
    return [
        space
        for space in spaces
        if all(
            token in normalize_space_name(space.get("displayName") or "")
            for token in tokens
        )
    ]


def format_space_line(space: dict) -> str:
    space_name = space.get("displayName", "Unnamed Space")
    space_id = space.get("name", "")
    space_type_actual = space.get("spaceType", "UNKNOWN")
    last_active = space.get("lastActiveTime") or space.get("createTime") or "unknown"
    return (
        f"- {space_name} (ID: {space_id}, Type: {space_type_actual}, "
        f"Last active: {last_active})"
    )


def names_match_candidates(display_name: str, candidates: List[str]) -> bool:
    normalized_display = normalize_space_name(display_name)
    normalized_candidates = {normalize_space_name(name) for name in candidates}
    return normalized_display in normalized_candidates


def extract_space_ids_from_text(text: str) -> Set[str]:
    return set(_CHAT_SPACE_ID_PATTERN.findall(text or ""))
