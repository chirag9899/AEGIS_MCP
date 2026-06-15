"""
Google Chat MCP Tools

This module provides MCP tools for interacting with Google Chat API.
"""

import base64
import logging
import asyncio
import re
import ssl
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

import httpx
from googleapiclient.errors import HttpError

from mcp.types import ToolAnnotations

# Auth & server utilities
from auth.service_decorator import require_google_service, require_multiple_services
from core.server import server
from core.utils import TransientNetworkError, handle_http_errors
from gchat.space_discovery import (
    add_candidate_ids,
    build_display_name_candidates,
    extract_space_id_from_url,
    extract_space_ids_from_text,
    filter_spaces_by_display_name,
    format_space_line,
    lookup_registry_by_event,
    lookup_registry_by_name,
    names_match_candidates,
    normalize_space_resource_name,
    parse_date_from_text,
    register_space,
)

logger = logging.getLogger(__name__)

# In-memory cache for user ID → display name (bounded to avoid unbounded growth)
_SENDER_CACHE_MAX_SIZE = 256
_sender_name_cache: Dict[str, str] = {}
_SEARCH_MESSAGES_MAX_CONCURRENT_SPACE_FETCHES = 1
_SEARCH_MESSAGES_SSL_RETRIES = 3
_SEARCH_MESSAGES_RETRY_BASE_DELAY_SECONDS = 1
_LIST_SPACES_API_PAGE_SIZE = 1000
_LIST_SPACES_MAX_PAGES = 20
_FIND_CHAT_SPACE_PROBE_LIMIT = 40
_ADMIN_SEARCH_PAGE_SIZE = 25
_ADMIN_SEARCH_MAX_PAGES = 4


def _cache_sender(user_id: str, name: str) -> None:
    """Store a resolved sender name, evicting oldest entries if cache is full."""
    if len(_sender_name_cache) >= _SENDER_CACHE_MAX_SIZE:
        to_remove = list(_sender_name_cache.keys())[: _SENDER_CACHE_MAX_SIZE // 2]
        for k in to_remove:
            del _sender_name_cache[k]
    _sender_name_cache[user_id] = name


async def _resolve_sender(people_service, sender_obj: dict) -> str:
    """Resolve a Chat message sender to a display name.

    Fast path: use displayName if the API already provided it.
    Slow path: look up the user via the People API directory and cache the result.
    """
    # Fast path — Chat API sometimes provides displayName directly
    display_name = sender_obj.get("displayName")
    if display_name:
        return display_name

    user_id = sender_obj.get("name", "")  # e.g. "users/123456789"
    if not user_id:
        return "Unknown Sender"

    # Check cache
    if user_id in _sender_name_cache:
        return _sender_name_cache[user_id]

    # Try People API directory lookup
    # Chat API uses "users/ID" but People API expects "people/ID"
    people_resource = user_id.replace("users/", "people/", 1)
    if people_service:
        try:
            person = await asyncio.to_thread(
                people_service.people()
                .get(resourceName=people_resource, personFields="names,emailAddresses")
                .execute
            )
            names = person.get("names", [])
            if names:
                resolved = names[0].get("displayName", user_id)
                _cache_sender(user_id, resolved)
                return resolved
            # Fall back to email if no name
            emails = person.get("emailAddresses", [])
            if emails:
                resolved = emails[0].get("value", user_id)
                _cache_sender(user_id, resolved)
                return resolved
        except HttpError as e:
            logger.debug(f"People API lookup failed for {user_id}: {e}")
        except Exception as e:
            logger.debug(f"Unexpected error resolving {user_id}: {e}")

    # Final fallback
    _cache_sender(user_id, user_id)
    return user_id


async def _execute_chat_request(
    request_factory,
    *,
    request_label: str,
    retries: int = 1,
    semaphore: Optional[asyncio.Semaphore] = None,
):
    """Execute a Chat API request in a worker thread with optional SSL retries."""
    for attempt in range(retries):
        try:
            if semaphore is None:
                return await asyncio.to_thread(lambda: request_factory().execute())
            async with semaphore:
                return await asyncio.to_thread(lambda: request_factory().execute())
        except ssl.SSLError as e:
            if attempt == retries - 1:
                raise
            delay = _SEARCH_MESSAGES_RETRY_BASE_DELAY_SECONDS * (2**attempt)
            logger.warning(
                "[search_messages] SSL error during %s on attempt %s/%s: %s. Retrying in %s seconds.",
                request_label,
                attempt + 1,
                retries,
                e,
                delay,
            )
            await asyncio.sleep(delay)


def _extract_rich_links(msg: dict) -> List[str]:
    """Extract URLs from RICH_LINK annotations (smart chips).

    When a user pastes a Google Workspace URL in Chat and it renders as a
    smart chip, the URL is NOT in the text field — it's only available in
    the annotations array as a RICH_LINK with richLinkMetadata.uri.
    """
    text = msg.get("text", "")
    urls = []
    for ann in msg.get("annotations", []):
        if ann.get("type") == "RICH_LINK":
            uri = ann.get("richLinkMetadata", {}).get("uri", "")
            if uri and uri not in text:
                urls.append(uri)
    return urls


async def _fetch_all_spaces(service, *, filter_param: Optional[str] = None) -> List[dict]:
    spaces: List[dict] = []
    page_token: Optional[str] = None
    for _ in range(_LIST_SPACES_MAX_PAGES):
        request_params: Dict[str, object] = {"pageSize": _LIST_SPACES_API_PAGE_SIZE}
        if filter_param:
            request_params["filter"] = filter_param
        if page_token:
            request_params["pageToken"] = page_token
        response = await asyncio.to_thread(
            service.spaces().list(**request_params).execute
        )
        spaces.extend(response.get("spaces", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return spaces


async def _get_space_dict(service, space_id: str) -> dict:
    resource = normalize_space_resource_name(space_id)
    return await asyncio.to_thread(service.spaces().get(name=resource).execute)


def _build_admin_search_query(display_name_candidates: List[str]) -> Optional[str]:
    """Build a Workspace-admin spaces.search query from display-name candidates."""
    phrases: List[str] = []
    seen: Set[str] = set()
    for candidate in sorted(display_name_candidates, key=len, reverse=True):
        phrase = " ".join(candidate.split())
        if len(phrase) < 3 or phrase.lower() in seen:
            continue
        seen.add(phrase.lower())
        phrases.append(phrase)
        if len(phrases) >= 3:
            break
    if not phrases:
        return None
    display_clause = " OR ".join(f'displayName:"{phrase}"' for phrase in phrases)
    if len(phrases) > 1:
        display_clause = f"({display_clause})"
    return (
        'customer = "customers/my_customer" AND spaceType = "SPACE" AND '
        f"{display_clause}"
    )


async def _admin_search_spaces(
    chat_service,
    *,
    display_name_candidates: List[str],
) -> List[dict]:
    """Search all org spaces via admin API (finds inactive Meet rooms)."""
    query = _build_admin_search_query(display_name_candidates)
    if not query:
        return []

    matches: List[dict] = []
    page_token: Optional[str] = None
    for _ in range(_ADMIN_SEARCH_MAX_PAGES):
        request_params = {
            "query": query,
            "useAdminAccess": True,
            "pageSize": _ADMIN_SEARCH_PAGE_SIZE,
        }
        if page_token:
            request_params["pageToken"] = page_token
        try:
            response = await asyncio.to_thread(
                chat_service.spaces().search(**request_params).execute
            )
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status in (403, 401):
                logger.info(
                    "[find_chat_space] Admin spaces.search unavailable (HTTP %s): %s",
                    status,
                    exc,
                )
            else:
                logger.warning(
                    "[find_chat_space] Admin spaces.search failed: %s", exc
                )
            return []
        except Exception as exc:
            logger.warning("[find_chat_space] Admin spaces.search failed: %s", exc)
            return []

        for space in response.get("spaces", []):
            display_name = space.get("displayName", "")
            if names_match_candidates(display_name, display_name_candidates):
                matches.append(space)

        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return _dedupe_spaces(matches)


async def _find_calendar_event(
    calendar_service,
    *,
    query: str,
    event_date: Optional[str],
) -> Optional[dict]:
    if not event_date:
        return None
    try:
        start = datetime.strptime(event_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    end = start + timedelta(days=1)
    request_params = {
        "calendarId": "primary",
        "timeMin": start.isoformat().replace("+00:00", "Z"),
        "timeMax": end.isoformat().replace("+00:00", "Z"),
        "singleEvents": True,
        "maxResults": 10,
    }
    if query:
        request_params["q"] = query
    response = await asyncio.to_thread(
        calendar_service.events().list(**request_params).execute
    )
    items = response.get("items", [])
    return items[0] if items else None


def _meet_code_from_event(event: dict) -> Optional[str]:
    hangout = event.get("hangoutLink") or ""
    match = re.search(r"meet\.google\.com/([a-z]+-[a-z]+-[a-z]+)", hangout, re.I)
    if match:
        return match.group(1).lower()
    for entry in event.get("conferenceData", {}).get("entryPoints", []):
        uri = entry.get("uri", "")
        match = re.search(r"meet\.google\.com/([a-z]+-[a-z]+-[a-z]+)", uri, re.I)
        if match:
            return match.group(1).lower()
    return None


async def _harvest_gmail_space_ids(gmail_service, *, query: str) -> Set[str]:
    discovered: Set[str] = set()
    try:
        response = await asyncio.to_thread(
            gmail_service.users()
            .messages()
            .list(userId="me", q=query, maxResults=15)
            .execute
        )
        for item in response.get("messages", []):
            message = await asyncio.to_thread(
                gmail_service.users()
                .messages()
                .get(userId="me", id=item["id"], format="raw")
                .execute
            )
            raw = base64.urlsafe_b64decode(message.get("raw", ""))
            discovered.update(
                extract_space_ids_from_text(raw.decode("utf-8", errors="replace"))
            )
    except Exception as exc:
        logger.debug("[find_chat_space] Gmail harvest skipped: %s", exc)
    return discovered


def _dedupe_spaces(spaces: List[dict]) -> List[dict]:
    deduped: List[dict] = []
    seen: Set[str] = set()
    for space in spaces:
        resource = space.get("name", "")
        if not resource or resource in seen:
            continue
        seen.add(resource)
        deduped.append(space)
    return deduped


def _format_find_chat_space_success(
    name: str,
    spaces: List[dict],
    sources: str,
    *,
    calendar_event: Optional[dict] = None,
) -> str:
    for space in spaces:
        register_space(
            space_id=space.get("name", ""),
            display_name=space.get("displayName", ""),
            meet_code=_meet_code_from_event(calendar_event) if calendar_event else None,
            event_instance_id=calendar_event.get("id") if calendar_event else None,
        )
    lines = [f"Found {len(spaces)} matching space(s) for '{name}' (via {sources}):"]
    lines.extend(format_space_line(space) for space in spaces)
    lines.append(
        "\nUse get_messages with the space ID above. "
        "Meet-linked rooms may have zero messages until chat activity occurs."
    )
    return "\n".join(lines)


async def _probe_spaces_for_names(
    chat_service,
    *,
    candidate_ids: List[str],
    display_name_candidates: List[str],
) -> List[dict]:
    matches: List[dict] = []
    seen: Set[str] = set()
    for bare_id in candidate_ids[:_FIND_CHAT_SPACE_PROBE_LIMIT]:
        resource = normalize_space_resource_name(bare_id)
        if resource in seen:
            continue
        seen.add(resource)
        try:
            space = await _get_space_dict(chat_service, resource)
        except Exception:
            continue
        display_name = space.get("displayName", "")
        if names_match_candidates(display_name, display_name_candidates):
            matches.append(space)
            register_space(
                space_id=resource,
                display_name=display_name,
            )
    return matches


@server.tool(
    title="List Spaces",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_google_service("chat", "chat_spaces_readonly")
@handle_http_errors("list_spaces", service_type="chat")
async def list_spaces(
    service,
    user_google_email: str,
    page_size: int = 100,
    space_type: str = "all",  # "all", "room", "dm"
) -> str:
    """
    Lists Google Chat spaces (rooms and direct messages) accessible to the user.

    Returns:
        str: A formatted list of Google Chat spaces accessible to the user.
    """
    logger.info(f"[list_spaces] Email={user_google_email}, Type={space_type}")

    # Build filter based on space_type
    filter_param = None
    if space_type == "room":
        filter_param = "spaceType = SPACE"
    elif space_type == "dm":
        filter_param = "spaceType = DIRECT_MESSAGE"

    request_params = {"pageSize": page_size}
    if filter_param:
        request_params["filter"] = filter_param

    response = await asyncio.to_thread(service.spaces().list(**request_params).execute)

    spaces = response.get("spaces", [])
    if not spaces:
        return f"No Chat spaces found for type '{space_type}'."

    output = [f"Found {len(spaces)} Chat spaces (type: {space_type}):"]
    for space in spaces:
        space_name = space.get("displayName", "Unnamed Space")
        space_id = space.get("name", "")
        space_type_actual = space.get("spaceType", "UNKNOWN")
        output.append(f"- {space_name} (ID: {space_id}, Type: {space_type_actual})")

    return "\n".join(output)


@server.tool(
    title="Get Chat Space",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_google_service("chat", "chat_spaces_readonly")
@handle_http_errors("get_space", service_type="chat")
async def get_space(
    service,
    user_google_email: str,
    space_id_or_url: str,
) -> str:
    """
    Returns metadata for a Google Chat space by ID or Chat URL.

    Accepts a resource name (`spaces/AAQA1k9BNCE`), bare ID (`AAQA1k9BNCE`),
    or a Gmail/Chat URL containing `#chat/space/...` or `chat.google.com/room/...`.
    Successful lookups are cached for future `find_chat_space` calls.
    """
    extracted = extract_space_id_from_url(space_id_or_url)
    if not extracted:
        return (
            "Could not parse a Chat space ID from the input. Provide `spaces/...`, "
            "a bare space ID, or a Chat URL such as "
            "`https://mail.google.com/mail/u/0/#chat/space/AAQA1k9BNCE`."
        )

    resource = normalize_space_resource_name(extracted)
    logger.info("[get_space] Email=%s, Space=%s", user_google_email, resource)

    space = await _get_space_dict(service, resource)
    display_name = space.get("displayName", "Unnamed Space")
    register_space(space_id=resource, display_name=display_name)
    add_candidate_ids({resource})

    lines = [
        f"Space: {display_name}",
        f"ID: {space.get('name', resource)}",
        f"Type: {space.get('spaceType', 'UNKNOWN')}",
        f"Last active: {space.get('lastActiveTime') or 'unknown'}",
        f"Created: {space.get('createTime') or 'unknown'}",
    ]
    space_uri = space.get("spaceUri") or ""
    if space_uri:
        lines.append(f"URI: {space_uri}")
    lines.append(
        "Cached for future lookups via find_chat_space. "
        "Use get_messages with this ID even if the space has no messages yet."
    )
    return "\n".join(lines)


@server.tool(
    title="Find Chat Space",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_multiple_services(
    [
        {"service_type": "chat", "scopes": "chat_spaces_readonly", "param_name": "chat_service"},
        {"service_type": "calendar", "scopes": "calendar_read", "param_name": "calendar_service"},
        {"service_type": "gmail", "scopes": "gmail_read", "param_name": "gmail_service"},
    ]
)
@handle_http_errors("find_chat_space", service_type="chat")
async def find_chat_space(
    chat_service,
    calendar_service,
    gmail_service,
    user_google_email: str,
    name: str,
    event_date: Optional[str] = None,
    chat_url: Optional[str] = None,
) -> str:
    """
    Find a Google Chat space by display name (e.g. "Daily Sync – Jun 11").

    Uses multiple strategies because Meet-linked spaces with no chat messages are
    often missing from spaces.list but are still accessible by ID:
    1. Shared org registry (populated by prior get_space / find calls)
    2. Workspace-admin spaces.search (requires chat.admin.spaces.readonly)
    3. Full spaces.list pagination + name filter
    4. Calendar event lookup for the matching date
    5. Gmail scan + direct spaces.get probes on known candidate IDs

    Args:
        name: Space title or phrase, e.g. "Daily Sync Jun 11".
        event_date: Optional ISO date (`2026-06-11`) when not present in name.
        chat_url: Optional Chat/Gmail URL to register and return immediately.

    Returns:
        str: Matching space(s) with IDs, or calendar context when only Meet is known.
    """
    logger.info(
        "[find_chat_space] Email=%s, Name=%s, Date=%s, URL=%s",
        user_google_email,
        name,
        event_date,
        bool(chat_url),
    )

    if chat_url:
        extracted = extract_space_id_from_url(chat_url)
        if extracted:
            space = await _get_space_dict(chat_service, extracted)
            register_space(
                space_id=space.get("name", normalize_space_resource_name(extracted)),
                display_name=space.get("displayName", name),
            )
            return (
                f"Found space from URL:\n"
                f"{format_space_line(space)}\n\n"
                "Cached for future name-based lookups."
            )

    parsed_date = event_date or parse_date_from_text(name)
    display_name_candidates = build_display_name_candidates(name, parsed_date)
    matches: List[dict] = []
    match_sources: List[str] = []

    for candidate in display_name_candidates:
        resource = lookup_registry_by_name(candidate)
        if not resource:
            continue
        try:
            space = await _get_space_dict(chat_service, resource)
            matches.append(space)
            match_sources.append("registry")
        except Exception:
            continue

    if not matches:
        admin_matches = await _admin_search_spaces(
            chat_service,
            display_name_candidates=display_name_candidates,
        )
        if admin_matches:
            matches.extend(admin_matches)
            match_sources.append("admin search")

    if not matches:
        listed = await _fetch_all_spaces(chat_service)
        add_candidate_ids({space.get("name", "") for space in listed})
        for candidate in display_name_candidates:
            matches.extend(filter_spaces_by_display_name(listed, candidate))
        if matches:
            match_sources.append("spaces.list")

    if deduped := _dedupe_spaces(matches):
        sources = ", ".join(sorted(set(match_sources))) or "lookup"
        return _format_find_chat_space_success(name, deduped, sources)

    calendar_event = await _find_calendar_event(
        calendar_service,
        query=name.split("-")[0].strip() or "Daily Sync",
        event_date=parsed_date,
    )
    if calendar_event and not matches:
        resource = lookup_registry_by_event(calendar_event.get("id", ""))
        if resource:
            try:
                matches.append(await _get_space_dict(chat_service, resource))
                match_sources.append("registry+calendar")
            except Exception:
                pass

    if not matches:
        candidate_ids: List[str] = []
        gmail_query = name
        if parsed_date:
            gmail_query = f'{name} after:{parsed_date.replace("-", "/")}'
        candidate_ids.extend(
            sorted(await _harvest_gmail_space_ids(gmail_service, query=gmail_query))
        )
        from gchat.space_discovery import load_registry

        candidate_ids.extend(load_registry().get("candidate_ids", []))
        probed = await _probe_spaces_for_names(
            chat_service,
            candidate_ids=candidate_ids,
            display_name_candidates=display_name_candidates,
        )
        if probed:
            matches.extend(probed)
            match_sources.append("direct lookup")

    deduped = _dedupe_spaces(matches)
    if deduped:
        sources = ", ".join(sorted(set(match_sources))) or "lookup"
        return _format_find_chat_space_success(
            name,
            deduped,
            sources,
            calendar_event=calendar_event,
        )

    lines = [f"No Chat space found for '{name}'."]
    if calendar_event:
        meet_code = _meet_code_from_event(calendar_event)
        lines.append("\nMatching calendar event:")
        lines.append(f"- Title: {calendar_event.get('summary', 'Untitled')}")
        lines.append(f"- Start: {calendar_event.get('start', {})}")
        if calendar_event.get("hangoutLink"):
            lines.append(f"- Meet link: {calendar_event['hangoutLink']}")
        if meet_code:
            lines.append(f"- Meet code: {meet_code}")
        lines.append(
            "\nThe Meet event exists, but Google does not expose the Chat space ID "
            "in Calendar. Open the meeting chat once and call get_space with the "
            "Chat URL, or pass chat_url to find_chat_space. The ID is then cached "
            "for the whole team."
        )
    else:
        lines.append(
            "\nTried spaces.list, the shared registry, Gmail, and direct space lookup. "
            "If you have the Chat URL, pass it as chat_url or use get_space."
        )
    return "\n".join(lines)


@server.tool(
    title="Get Messages",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_multiple_services(
    [
        {"service_type": "chat", "scopes": "chat_read", "param_name": "chat_service"},
        {
            "service_type": "people",
            "scopes": "contacts_read",
            "param_name": "people_service",
        },
    ]
)
@handle_http_errors("get_messages", service_type="chat")
async def get_messages(
    chat_service,
    people_service,
    user_google_email: str,
    space_id: str,
    page_size: int = 50,
    order_by: str = "createTime desc",
    message_filter: Optional[str] = None,
) -> str:
    """
    Retrieves messages from a Google Chat space.

    Args:
        message_filter: Optional filter string using the Chat API filter syntax.
                        Supports createTime and thread.name.
                        Examples:
                          'createTime > "2026-03-18T00:00:00-03:00"'
                          'createTime > "2026-03-18T00:00:00-03:00" AND createTime < "2026-03-19T00:00:00-03:00"'
                          'thread.name = spaces/X/threads/Y'

    Returns:
        str: Formatted messages from the specified space.
    """
    logger.info(f"[get_messages] Space ID: '{space_id}' for user '{user_google_email}'")

    # Get space info first
    space_info = await asyncio.to_thread(
        chat_service.spaces().get(name=space_id).execute
    )
    space_name = space_info.get("displayName", "Unknown Space")

    # Get messages
    list_params = {"parent": space_id, "pageSize": page_size, "orderBy": order_by}
    if message_filter is not None:
        list_params["filter"] = message_filter
    response = await asyncio.to_thread(
        chat_service.spaces().messages().list(**list_params).execute
    )

    messages = response.get("messages", [])
    if not messages:
        return f"No messages found in space '{space_name}' (ID: {space_id})."

    # Pre-resolve unique senders sequentially. The underlying googleapiclient/httplib2
    # service objects are not safe to fan out across worker threads.
    sender_lookup = {}
    for msg in messages:
        s = msg.get("sender", {})
        key = s.get("name", "")
        if key and key not in sender_lookup:
            sender_lookup[key] = s
    sender_map = {}
    for key, sender_obj in sender_lookup.items():
        sender_map[key] = await _resolve_sender(people_service, sender_obj)

    output = [f"Messages from '{space_name}' (ID: {space_id}):\n"]
    for msg in messages:
        sender_obj = msg.get("sender", {})
        sender_key = sender_obj.get("name", "")
        sender = sender_map.get(sender_key) or await _resolve_sender(
            people_service, sender_obj
        )
        create_time = msg.get("createTime", "Unknown Time")
        text_content = msg.get("text", "No text content")
        msg_name = msg.get("name", "")

        output.append(f"[{create_time}] {sender}:")
        output.append(f"  {text_content}")
        rich_links = _extract_rich_links(msg)
        for url in rich_links:
            output.append(f"  [linked: {url}]")
        # Show attachments
        attachments = msg.get("attachment", [])
        for idx, att in enumerate(attachments):
            att_name = att.get("contentName", "unnamed")
            att_type = att.get("contentType", "unknown type")
            att_resource = att.get("name", "")
            output.append(f"  [attachment {idx}: {att_name} ({att_type})]")
            if att_resource:
                output.append(
                    f"  Use download_chat_attachment(message_id='{msg_name}', attachment_index={idx}) to download"
                )
        # Show thread info if this is a threaded reply
        thread = msg.get("thread", {})
        if msg.get("threadReply") and thread.get("name"):
            output.append(f"  [thread: {thread['name']}]")
        # Show emoji reactions
        reactions = msg.get("emojiReactionSummaries", [])
        if reactions:
            parts = []
            for r in reactions:
                emoji = r.get("emoji", {})
                symbol = emoji.get("unicode", "")
                if not symbol:
                    ce = emoji.get("customEmoji", {})
                    symbol = f":{ce.get('uid', '?')}:"
                count = r.get("reactionCount", 0)
                parts.append(f"{symbol}x{count}")
            output.append(f"  [reactions: {', '.join(parts)}]")
        output.append(f"  (Message ID: {msg_name})\n")

    return "\n".join(output)


@server.tool(
    title="Send Message",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@require_google_service("chat", "chat_write")
@handle_http_errors("send_message", service_type="chat")
async def send_message(
    service,
    user_google_email: str,
    space_id: str,
    message_text: str,
    thread_key: Optional[str] = None,
    thread_name: Optional[str] = None,
) -> str:
    """
    Sends a message to a Google Chat space.

    Args:
        thread_name: Reply in an existing thread by its resource name (e.g. spaces/X/threads/Y).
        thread_key: Reply in a thread by app-defined key (creates thread if not found).

    Returns:
        str: Confirmation message with sent message details.
    """
    logger.info(f"[send_message] Email: '{user_google_email}', Space: '{space_id}'")

    message_body = {"text": message_text}

    request_params = {"parent": space_id, "body": message_body}

    # Thread reply support
    if thread_name:
        message_body["thread"] = {"name": thread_name}
        request_params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    elif thread_key:
        message_body["thread"] = {"threadKey": thread_key}
        request_params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

    message = await asyncio.to_thread(
        service.spaces().messages().create(**request_params).execute
    )

    message_name = message.get("name", "")
    create_time = message.get("createTime", "")

    msg = f"Message sent to space '{space_id}' by {user_google_email}. Message ID: {message_name}, Time: {create_time}"
    logger.info(
        f"Successfully sent message to space '{space_id}' by {user_google_email}"
    )
    return msg


@server.tool(
    title="Search Messages",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@require_multiple_services(
    [
        {"service_type": "chat", "scopes": "chat_read", "param_name": "chat_service"},
        {
            "service_type": "people",
            "scopes": "contacts_read",
            "param_name": "people_service",
        },
    ]
)
@handle_http_errors("search_messages", is_read_only=True, service_type="chat")
async def search_messages(
    chat_service,
    people_service,
    user_google_email: str,
    query: Optional[str] = None,
    space_id: Optional[str] = None,
    page_size: int = 25,
    time_filter: Optional[str] = None,
    max_spaces: int = 10,
) -> str:
    """
    Searches for messages in Google Chat spaces by text content and/or time range.

    Args:
        query: Optional text to search for. If omitted, only time_filter is applied.
        space_id: Optional space to restrict the search to.
        page_size: Maximum number of messages to return per space.
        time_filter: Optional filter using Chat API createTime syntax.
                     Examples:
                       'createTime > "2026-03-18T00:00:00-03:00"'
                       'createTime > "2026-03-18T00:00:00-03:00" AND createTime < "2026-03-19T00:00:00-03:00"'
        max_spaces: Maximum number of spaces to search when space_id is not provided (default 10).

    Returns:
        str: A formatted list of messages matching the search criteria.
    """
    logger.info(
        f"[search_messages] Email={user_google_email}, Query='{query}', TimeFilter='{time_filter}'"
    )

    # Google Chat messages.list supports time/thread filters, but not full-text
    # search. Apply only supported API filters, then filter message text below.
    filter_parts = []
    if time_filter:
        filter_parts.append(time_filter)
    filter_str = " AND ".join(filter_parts) if filter_parts else None

    search_terms = []
    if query:
        search_terms.append(f'text "{query}"')
    if time_filter:
        search_terms.append(time_filter)
    search_desc = " and ".join(search_terms) if search_terms else "all messages"

    # If specific space provided, search within that space
    if space_id:
        list_params = {"parent": space_id, "pageSize": page_size}
        if filter_str:
            list_params["filter"] = filter_str
        response = await _execute_chat_request(
            lambda: chat_service.spaces().messages().list(**list_params),
            request_label=f"fetching messages for {space_id}",
            retries=_SEARCH_MESSAGES_SSL_RETRIES,
        )
        messages = response.get("messages", [])
        context = f"space '{space_id}'"
    else:
        # Search across all accessible spaces
        spaces_response = await _execute_chat_request(
            lambda: chat_service.spaces().list(pageSize=100),
            request_label="listing accessible spaces",
            retries=_SEARCH_MESSAGES_SSL_RETRIES,
        )
        spaces = spaces_response.get("spaces", [])
        spaces_to_search = spaces[:max_spaces]
        fetch_semaphore = asyncio.Semaphore(
            _SEARCH_MESSAGES_MAX_CONCURRENT_SPACE_FETCHES
        )

        async def fetch_space_messages(space: dict) -> tuple[List[dict], bool]:
            try:
                list_params = {"parent": space.get("name"), "pageSize": page_size}
                if filter_str:
                    list_params["filter"] = filter_str
                response = await _execute_chat_request(
                    lambda: chat_service.spaces().messages().list(**list_params),
                    request_label=f"fetching messages for {space.get('name')}",
                    retries=_SEARCH_MESSAGES_SSL_RETRIES,
                    semaphore=fetch_semaphore,
                )
                msgs = response.get("messages", [])
                display = space.get("displayName", "Unknown")
                for msg in msgs:
                    msg["_space_name"] = display
                return msgs, False
            except HttpError as e:
                logger.debug(
                    "Skipping space %s during search: %s", space.get("name"), e
                )
                return [], False
            except ssl.SSLError as e:
                logger.warning(
                    "Skipping space %s during search after repeated SSL failures: %s",
                    space.get("name"),
                    e,
                )
                return [], True

        results = await asyncio.gather(
            *(fetch_space_messages(space) for space in spaces_to_search)
        )
        transient_failures = 0
        messages = []
        for batch, had_transient_failure in results:
            messages.extend(batch)
            transient_failures += int(had_transient_failure)
        if spaces_to_search and transient_failures == len(spaces_to_search):
            raise TransientNetworkError(
                "A transient SSL error occurred in 'search_messages' while searching Chat spaces. "
                "Please try again shortly."
            )
        context = "all accessible spaces"

    # Client-side text filtering (text: operator is not supported by the API)
    if query:
        query_lower = query.lower()
        messages = [m for m in messages if query_lower in (m.get("text") or "").lower()]

    if not messages:
        suffix = (
            f" Skipped {transient_failures} spaces due to repeated SSL failures."
            if "transient_failures" in locals() and transient_failures
            else ""
        )
        return f"No messages found matching '{search_desc}' in {context}.{suffix}"

    # Resolve senders sequentially. The underlying googleapiclient/httplib2
    # service objects are not safe to fan out heavily and can trigger SSL churn.
    sender_lookup = {}
    for msg in messages:
        s = msg.get("sender", {})
        key = s.get("name", "")
        if key and key not in sender_lookup:
            sender_lookup[key] = s
    sender_map = {}
    for key, sender_obj in sender_lookup.items():
        sender_map[key] = await _resolve_sender(people_service, sender_obj)

    output = [f"Found {len(messages)} messages matching '{search_desc}' in {context}:"]
    for msg in messages:
        sender_obj = msg.get("sender", {})
        sender_key = sender_obj.get("name", "")
        sender = sender_map.get(sender_key) or await _resolve_sender(
            people_service, sender_obj
        )
        create_time = msg.get("createTime", "Unknown Time")
        text_content = msg.get("text", "No text content")
        space_name = msg.get("_space_name", "Unknown Space")

        # Truncate long messages
        if len(text_content) > 100:
            text_content = text_content[:100] + "..."

        rich_links = _extract_rich_links(msg)
        links_suffix = "".join(f" [linked: {url}]" for url in rich_links)
        attachments = msg.get("attachment", [])
        att_suffix = "".join(
            f" [attachment: {a.get('contentName', 'unnamed')} ({a.get('contentType', 'unknown type')})]"
            for a in attachments
        )
        output.append(
            f"- [{create_time}] {sender} in '{space_name}': {text_content}{links_suffix}{att_suffix}"
        )

    return "\n".join(output)


@server.tool(
    title="Create Reaction",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@require_google_service("chat", "chat_write")
@handle_http_errors("create_reaction", service_type="chat")
async def create_reaction(
    service,
    user_google_email: str,
    message_id: str,
    emoji_unicode: str,
) -> str:
    """
    Adds an emoji reaction to a Google Chat message.

    Args:
        message_id: The message resource name (e.g. spaces/X/messages/Y).
        emoji_unicode: The emoji character to react with (e.g. 👍).

    Returns:
        str: Confirmation message.
    """
    logger.info(f"[create_reaction] Message: '{message_id}', Emoji: '{emoji_unicode}'")

    reaction = await asyncio.to_thread(
        service.spaces()
        .messages()
        .reactions()
        .create(
            parent=message_id,
            body={"emoji": {"unicode": emoji_unicode}},
        )
        .execute
    )

    reaction_name = reaction.get("name", "")
    return f"Reacted with {emoji_unicode} on message {message_id}. Reaction ID: {reaction_name}"


@server.tool(
    title="Download Chat Attachment",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@handle_http_errors("download_chat_attachment", is_read_only=True, service_type="chat")
@require_google_service("chat", "chat_read")
async def download_chat_attachment(
    service,
    user_google_email: str,
    message_id: str,
    attachment_index: int = 0,
) -> str:
    """
    Downloads an attachment from a Google Chat message and saves it to local disk.

    In stdio mode, returns the local file path for direct access.
    In HTTP mode, returns a temporary download URL (valid for 1 hour).

    Args:
        message_id: The message resource name (e.g. spaces/X/messages/Y).
        attachment_index: Zero-based index of the attachment to download (default 0).

    Returns:
        str: Attachment metadata with either a local file path or download URL.
    """
    logger.info(
        f"[download_chat_attachment] Message: '{message_id}', Index: {attachment_index}"
    )

    # Fetch the message to get attachment metadata
    msg = await asyncio.to_thread(
        service.spaces().messages().get(name=message_id).execute
    )

    attachments = msg.get("attachment", [])
    if not attachments:
        return f"No attachments found on message {message_id}."

    if attachment_index < 0 or attachment_index >= len(attachments):
        return (
            f"Invalid attachment_index {attachment_index}. "
            f"Message has {len(attachments)} attachment(s) (0-{len(attachments) - 1})."
        )

    att = attachments[attachment_index]
    filename = att.get("contentName", "attachment")
    content_type = att.get("contentType", "application/octet-stream")
    source = att.get("source", "")

    # The media endpoint needs attachmentDataRef.resourceName (e.g.
    # "spaces/S/attachments/A"), NOT the attachment name which includes
    # the /messages/ segment and causes 400 errors.
    media_resource = att.get("attachmentDataRef", {}).get("resourceName", "")
    att_name = att.get("name", "")

    logger.info(
        f"[download_chat_attachment] Downloading '{filename}' ({content_type}), "
        f"source={source}, mediaResource={media_resource}, name={att_name}"
    )

    # Download the attachment binary data via the Chat API media endpoint.
    # We use httpx with the Bearer token directly because MediaIoBaseDownload
    # and AuthorizedHttp fail in OAuth 2.1 (no refresh_token). The attachment's
    # downloadUri points to chat.google.com which requires browser cookies.
    if not media_resource and not att_name:
        return f"No resource name available for attachment '{filename}'."

    # Prefer attachmentDataRef.resourceName for the media endpoint
    resource_name = media_resource or att_name
    download_url = f"https://chat.googleapis.com/v1/media/{resource_name}?alt=media"

    try:
        access_token = service._http.credentials.token
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(
                download_url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code != 200:
                body = resp.text[:500]
                return (
                    f"Failed to download attachment '{filename}': "
                    f"HTTP {resp.status_code} from {download_url}\n{body}"
                )
            file_bytes = resp.content
    except Exception as e:
        return f"Failed to download attachment '{filename}': {e}"

    size_bytes = len(file_bytes)
    size_kb = size_bytes / 1024

    # Check if we're in stateless mode (can't save files)
    from auth.oauth_config import is_stateless_mode

    if is_stateless_mode():
        b64_preview = base64.urlsafe_b64encode(file_bytes).decode("utf-8")[:100]
        return "\n".join(
            [
                f"Attachment downloaded: {filename} ({content_type})",
                f"Size: {size_kb:.1f} KB ({size_bytes} bytes)",
                "",
                "Stateless mode: File storage disabled.",
                f"Base64 preview: {b64_preview}...",
            ]
        )

    # Save to local disk
    from core.attachment_storage import get_attachment_storage, get_attachment_url
    from core.config import get_transport_mode

    storage = get_attachment_storage()
    b64_data = base64.urlsafe_b64encode(file_bytes).decode("utf-8")
    result = storage.save_attachment(
        base64_data=b64_data, filename=filename, mime_type=content_type
    )

    result_lines = [
        f"Attachment downloaded: {filename}",
        f"Type: {content_type}",
        f"Size: {size_kb:.1f} KB ({size_bytes} bytes)",
    ]

    if get_transport_mode() == "stdio":
        result_lines.append(f"\nSaved to: {result.path}")
        result_lines.append(
            "\nThe file has been saved to disk and can be accessed directly via the file path."
        )
    else:
        download_url = get_attachment_url(result.file_id)
        result_lines.append(f"\nDownload URL: {download_url}")
        result_lines.append("\nThe file will expire after 1 hour.")

    logger.info(
        f"[download_chat_attachment] Saved {size_kb:.1f} KB attachment to {result.path}"
    )
    return "\n".join(result_lines)
