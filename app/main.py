from fastapi import FastAPI, Request
import requests, json

from app.config import settings
from app.db import get_db_conn, init_db
from app.rules_engine import get_response

app = FastAPI()

@app.on_event("startup")
def startup_event():
    init_db()


def send_whatsapp_message(payload: dict):
    url = f"{settings.base_url}/v1/messages"
    headers = {
        "D360-API-KEY": settings.d360_api_key,
        "Content-Type": "application/json"
    }
    print("📤 Sending WhatsApp message:", json.dumps(payload, indent=2))
    r = requests.post(url, headers=headers, json=payload)
    print("📥 WhatsApp API response:", r.status_code, r.text)
    return r.json()


@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    data = await request.json()
    print("Incoming:", json.dumps(data, indent=2))

    try:
        entry = data["entry"][0]["changes"][0]["value"]
        msg = entry["messages"][0]
        from_number = msg["from"]

        if msg["type"] == "text":
            user_input = msg["text"]["body"]
        elif msg["type"] == "interactive":
            if "button_reply" in msg["interactive"]:
                user_input = msg["interactive"]["button_reply"]["id"]
            elif "list_reply" in msg["interactive"]:
                user_input = msg["interactive"]["list_reply"]["id"]
            else:
                user_input = ""
        elif msg["type"] == "location":
            lat = msg.get("location", {}).get("latitude")
            lng = msg.get("location", {}).get("longitude")
            user_input = f"__location__:{lat},{lng}" if lat is not None and lng is not None else "__location__"
        else:
            user_input = ""

    except (KeyError, IndexError):
        return {"status": "ignored"}

    # --- Rule Engine ---
    payload, next_state = get_response(from_number, user_input)

    if not payload:
        # fallback if no match
        payload = {
            "messaging_product": "whatsapp",
            "to": from_number,
            "type": "text",
            "text": {"body": f"Echo: {user_input}"}
        }

    send_whatsapp_message(payload)

    return {"status": "ok"}
