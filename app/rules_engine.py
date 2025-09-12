import json
from pathlib import Path
import os
import json as _json
from typing import Optional
import redis
from app.config import settings
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Load flow JSON relative to this file for docker-friendly pathing
FLOW_PATH = Path(__file__).parent / "flow.json"
if not FLOW_PATH.exists():
    # Fallbacks for different working directories
    candidates = [
        Path.cwd() / "app" / "flow.json",
        Path.cwd() / "flow.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            FLOW_PATH = candidate
            break

if not FLOW_PATH.exists():
    raise FileNotFoundError(f"flow.json not found. Tried: {Path(__file__).parent / 'flow.json'}, {Path.cwd() / 'app' / 'flow.json'}, {Path.cwd() / 'flow.json'}; cwd={Path.cwd()}")

with open(FLOW_PATH, "r", encoding="utf-8") as f:
    FLOW = json.load(f)["nodes"]

# Simple in-memory fallback stores
USER_STATE: dict[str, str] = {}
USER_CONTEXT: dict[str, dict] = {}

_redis_client: Optional[redis.Redis] = None

def _get_redis() -> Optional[redis.Redis]:
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
        _redis_client.ping()
        return _redis_client
    except Exception:
        _redis_client = None
        return None

def _skey(sender: str, suffix: str) -> str:
    return f"session:{sender}:{suffix}"

def get_user_state(sender: str) -> str:
    r = _get_redis()
    if r:
        val = r.get(_skey(sender, "state"))
        if val:
            return val
    # fallback
    return USER_STATE.get(sender, "start")

def set_user_state(sender: str, node: str) -> None:
    r = _get_redis()
    if r:
        r.set(_skey(sender, "state"), node)
    USER_STATE[sender] = node

def get_user_context(sender: str) -> dict:
    r = _get_redis()
    if r:
        raw = r.get(_skey(sender, "ctx"))
        if raw:
            try:
                return _json.loads(raw)
            except Exception:
                return {}
    return USER_CONTEXT.get(sender, {}).copy()

def update_user_context(sender: str, updates: dict) -> dict:
    ctx = get_user_context(sender)
    ctx.update(updates)
    r = _get_redis()
    if r:
        r.set(_skey(sender, "ctx"), _json.dumps(ctx))
    USER_CONTEXT[sender] = ctx
    return ctx

def _replace_placeholders(text: str, ctx: dict) -> str:
    if not isinstance(text, str):
        return ""
    result = text
    for k, v in ctx.items():
        placeholder = f"{{{{{k}}}}}"
        result = result.replace(placeholder, str(v))
    # Backward-compat for username
    result = result.replace("{{username}}", ctx.get("username", "there"))
    return result


def build_payload(to: str, response: dict, ctx: dict):
    """
    Convert response block into 360dialog-compatible payload.
    """
    base = {
        "messaging_product": "whatsapp",
        "to": to,
    }

    if response["type"] == "text":
    
        # Replace basic placeholders; support both 'body' and 'text' keys in flow JSON
        raw = response.get("body") or response.get("text") or ""
        body_text = _replace_placeholders(raw, ctx)
        base["type"] = "text"
        base["text"] = {"body": body_text}
    
    elif response["type"] == "interactive":
        base["type"] = "interactive"
        # Case 1: WhatsApp list
        if "list" in response and isinstance(response["list"], dict):
            lst = response["list"]
            interactive: dict = {"type": "list"}
            # header/body/footer are optional in spec
            if "header" in lst:
                interactive["header"] = {
                    "type": lst["header"].get("type", "text"),
                    "text": _replace_placeholders(lst["header"].get("text", ""), ctx),
                }
            if "body" in lst:
                interactive["body"] = {"text": _replace_placeholders(lst["body"].get("text", ""), ctx)}
            if "footer" in lst:
                interactive["footer"] = {"text": _replace_placeholders(lst["footer"].get("text", ""), ctx)}
            action: dict = {"button": _replace_placeholders(lst.get("action", {}).get("button", "Select"), ctx), "sections": []}
            for section in lst.get("action", {}).get("sections", []):
                out_rows = []
                for row in section.get("rows", []):
                    # Strip internal fields like 'meta' and 'next' from what we send to WhatsApp
                    row_title = _replace_placeholders(row.get("title", ""), ctx)
                    row_desc = _replace_placeholders(row.get("description", ""), ctx) if row.get("description") else None
                    out_row = {"id": row.get("id", row_title), "title": row_title}
                    if row_desc:
                        out_row["description"] = row_desc
                    out_rows.append(out_row)
                action["sections"].append({
                    "title": _replace_placeholders(section.get("title", ""), ctx),
                    "rows": out_rows,
                })
            interactive["action"] = action
            base["interactive"] = interactive
        else:
            # Case 2: Fallback to interactive buttons (legacy)
            # Trim button titles to WhatsApp limit (<=20)
            buttons = []
            for b in response.get("buttons", []):
                title = _replace_placeholders(b.get("title", ""), ctx)
                if len(title) > 20:
                    title = title[:19] + "…"
                buttons.append({"type": "reply", "reply": {"id": b.get("id", title), "title": title}})
            base["interactive"] = {
                "type": "button",
                "body": {"text": _replace_placeholders(response.get("body", ""), ctx)},
                "action": {"buttons": buttons}
            }
    elif response["type"] == "template":
        # Fallback to text by rendering body_text with placeholders
        body = response.get("body_text") or response.get("body") or ""
        # Generate booking_id if needed
        if "{{booking_id}}" in body and "booking_id" not in ctx:
            ctx["booking_id"] = f"BK{int(datetime.now().timestamp())%1000000:06d}"
        body_text = _replace_placeholders(body, ctx)
        base["type"] = "text"
        base["text"] = {"body": body_text}
    # Default: if not a recognized/handled type, fall back to plain text to avoid API 400s
    if "type" not in base:
        fallback_text = _replace_placeholders(response.get("body") or response.get("text") or "Please continue.", ctx)
        base["type"] = "text"
        base["text"] = {"body": fallback_text}
    return base


def get_next_node(user_input: str, current_node: str = "start"):
    """
    Find the next node based on user input.
    """
    # Global reset: if user types start triggers or common reset words, go to start
    start_triggers = [t.lower() for t in FLOW.get("start", {}).get("triggers", [])]
    reset_words = ["reset", "restart", "start over", "menu", "hi", "hello"]
    normalized = user_input.lower() if isinstance(user_input, str) else ""
    if normalized in set(start_triggers + reset_words):
        return "start"

    node = FLOW[current_node]

    # Case 1: Text trigger within the node (stay on the same node)
    if "triggers" in node and normalized in [t.lower() for t in node["triggers"]]:
        return current_node

    # Case 2: Interactive button ID (buttons within response)
    if "response" in node and node["response"].get("type") == "interactive":
        resp = node["response"]
        # Support legacy buttons
        for btn in resp.get("buttons", []):
            matched = (user_input == btn.get("id")) or (isinstance(user_input, str) and user_input.lower() == str(btn.get("title", "")).lower())
            if matched:
                return btn.get("next")
        # Support list rows mapping
        lst = resp.get("list")
        if isinstance(lst, dict):
            for section in lst.get("action", {}).get("sections", []):
                for row in section.get("rows", []):
                    matched = (user_input == row.get("id")) or (isinstance(user_input, str) and user_input.lower() == str(row.get("title", "")).lower())
                    if matched:
                        return row.get("next")

    # Case 2b: Buttons defined at node level (e.g., text response + buttons)
    if node.get("buttons"):
        for btn in node["buttons"]:
            if user_input == btn.get("id"):
                return btn.get("next")
            if isinstance(user_input, str) and user_input.lower() == str(btn.get("title", "")).lower():
                return btn.get("next")

    # Case 3: Text entry with validation rules
    if node.get("response", {}).get("type") == "text" and node.get("validation"):
        validation = node["validation"]
        vtype = validation.get("type")
        if vtype == "regex":
            import re
            pattern = validation.get("pattern", "")
            if re.match(pattern, user_input or ""):
                return node.get("next_on_valid")
            else:
                return "__invalid__"
        if vtype == "min_length":
            min_len = int(validation.get("min", 0))
            if isinstance(user_input, str) and len(user_input.strip()) >= min_len:
                return node.get("on_receive", {}).get("next") or node.get("next_on_valid")
            else:
                return "__invalid__"

    # Case 4: Expected message types like 'location'
    if node.get("expected") and isinstance(node.get("expected"), list):
        # Location token from webhook
        if "location" in node["expected"] and isinstance(user_input, str) and user_input.startswith("__location__:"):
            return node.get("on_receive", {}).get("next")

    return None


def get_response(to: str, user_input: str, current_node: str = "start"):
    """
    Process user input, find next node, return WhatsApp payload + next node name.
    Uses in-memory USER_STATE to track conversation flow.
    """
    # Prefer stored state over provided default/current
    active_node = get_user_state(to) or current_node or "start"
    ctx = get_user_context(to)
    node = FLOW[active_node]
    next_node = get_next_node(user_input, active_node)
    if next_node == "__invalid__":
        # Validation failed: prefer validation.invalid_message; else node.fallback.text
        invalid_message = node.get("validation", {}).get("invalid_message") or node.get("fallback", {}).get("text") or "Input not recognized."
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": invalid_message},
        }
        return payload, active_node
    if not next_node:
        # No transition: send node fallback if available
        fb_text = node.get("fallback", {}).get("text")
        if fb_text:
            payload = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": fb_text},
            }
            return payload, active_node
        return None, None

    # If current node is interactive and a button with meta was chosen, capture meta into context
    if node.get("response", {}).get("type") == "interactive":
        resp = node.get("response", {})
        # Buttons meta capture
        for btn in resp.get("buttons", []):
            matched = (
                user_input == btn.get("id") or (
                    isinstance(user_input, str) and user_input.lower() == str(btn.get("title", "")).lower()
                )
            )
            if matched and isinstance(btn, dict) and isinstance(btn.get("meta"), dict):
                ctx.update(btn["meta"])  # e.g., {"direction": "round"}
            if matched:
                # Save selected values for well-known nodes
                if active_node == "choose_route":
                    ctx["route_title"] = btn.get("title")
                    ctx["route_id"] = btn.get("id")
                    # Derive route endpoints for readable direction formatting
                    title = ctx.get("route_title", "")
                    if "↔" in title:
                        ends = [p.strip() for p in title.split("↔", 1)]
                    elif "<->" in title:
                        ends = [p.strip() for p in title.split("<->", 1)]
                    else:
                        ends = [title.strip(), title.strip()]
                    if len(ends) == 2:
                        ctx["route_end_a"], ctx["route_end_b"] = ends[0], ends[1]
                if active_node == "choose_direction" and "meta" in btn and "direction" in btn["meta"]:
                    dir_key = btn["meta"]["direction"]
                    end_a = ctx.get("route_end_a")
                    end_b = ctx.get("route_end_b")
                    if dir_key == "AtoB" and end_a and end_b:
                        ctx["direction"] = f"{end_a} -> {end_b}"
                    elif dir_key == "BtoA" and end_a and end_b:
                        ctx["direction"] = f"{end_b} -> {end_a}"
                    elif dir_key == "round" and end_a and end_b:
                        ctx["direction"] = f"Round trip ({end_a} ↔ {end_b})"
                    else:
                        ctx["direction"] = dir_key
                break
        # List rows meta capture
        lst = resp.get("list")
        if isinstance(lst, dict):
            for section in lst.get("action", {}).get("sections", []):
                for row in section.get("rows", []):
                    matched = (
                        user_input == row.get("id") or (
                            isinstance(user_input, str) and user_input.lower() == str(row.get("title", "")).lower()
                        )
                    )
                    if matched and isinstance(row, dict) and isinstance(row.get("meta"), dict):
                        ctx.update(row["meta"])  # e.g., {"days_offset": 1}
                        break

    # If node defines on_receive.store_as for text or location input, store the value
    if node.get("on_receive", {}).get("store_as"):
        key_name = node["on_receive"]["store_as"]
        if isinstance(user_input, str) and user_input.startswith("__location__:"):
            _, coords = user_input.split(":", 1)
            ctx[key_name] = coords
        else:
            ctx[key_name] = user_input
        update_user_context(to, ctx)

    # Derive standard fields based on node and input
    tz = ZoneInfo("Africa/Johannesburg")
    if active_node == "date_selection":
        if user_input == "date_today":
            ctx["date"] = datetime.now(tz).strftime("%d/%m/%Y")
        if user_input == "date_tomorrow":
            ctx["date"] = (datetime.now(tz) + timedelta(days=1)).strftime("%d/%m/%Y")
    if active_node == "custom_date_entry" and next_node:
        # user_input is validated date
        ctx["date"] = user_input
    if active_node in ("custom_time_entry", "time_selection_return") and next_node:
        ctx["time"] = user_input
    if active_node == "type_address" and next_node:
        ctx["pickup_address_or_coords"] = ctx.get("pickup_address", user_input)
    if active_node == "capture_location" and next_node:
        ctx["pickup_address_or_coords"] = ctx.get("pickup_coords")

    # Resolve conditional nodes by evaluating simple "if x == 'y' then 'A' else 'B'"
    import re
    while True:
        candidate = FLOW.get(next_node, {})
        resp = candidate.get("response", {})
        if resp.get("type") != "conditional":
            break
        expr = candidate.get("response", {}).get("evaluate", "")
        # Very simple parser: if <var> == '<val>' then '<then>' else '<else>'
        m = re.match(r"\s*if\s+(\w+)\s*==\s*'([^']+)'\s*then\s*'([^']+)'\s*else\s*'([^']+)'\s*", expr)
        if not m:
            break
        var_name, expected, then_node, else_node = m.groups()
        var_value = ctx.get(var_name)
        next_node = then_node if str(var_value) == expected else else_node

    # Some nodes may not have a 'response' but might have top-level buttons, or be placeholders
    next_node_obj = FLOW.get(next_node, {})
    response = next_node_obj.get("response")
    if response:
        # If node has response text and separate buttons, render an interactive with that text and buttons
        if response.get("type") == "text" and next_node_obj.get("buttons"):
            payload = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": _replace_placeholders((response.get("body") or response.get("text") or "Please confirm."), ctx)},
                    "action": {"buttons": [
                        {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                        for b in next_node_obj["buttons"]
                    ]}
                }
            }
        else:
            payload = build_payload(to, response, ctx)
    else:
        # Try to build a simple interactive from 'buttons' if present, else fallback text
        if next_node_obj.get("buttons"):
            payload = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": _replace_placeholders(next_node_obj.get("body", "Please choose an option:"), ctx)},
                    "action": {"buttons": [
                        {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                        for b in next_node_obj["buttons"]
                    ]}
                }
            }
        else:
            payload = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": _replace_placeholders(next_node_obj.get("body") or "Please continue.", ctx)}
            }
    # Persist new state and context after resolving conditionals
    update_user_context(to, ctx)
    set_user_state(to, next_node)
    return payload, next_node
