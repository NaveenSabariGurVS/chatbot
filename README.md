# 360dialog WhatsApp Sandbox (FastAPI + Docker + Postgres)

Minimal FastAPI service to verify a 360dialog webhook, receive WhatsApp messages, store them in Postgres, and optionally echo replies.

## Stack
- FastAPI
- Postgres (psycopg2-binary)
- Docker + docker-compose
- Env-driven config via `.env`

## Environment variables (.env)
```
# 360dialog
D360_API_KEY=your-360dialog-api-key
BASE_URL=https://waba.360dialog.io
PHONE_NUMBER_ID=your-phone-number-id   # optional, not required for echo
VERIFY_TOKEN=change-me                 # used for GET /webhook verification

# Postgres
POSTGRES_USER=app
POSTGRES_PASSWORD=app
POSTGRES_DB=app
POSTGRES_HOST=db
POSTGRES_PORT=5432
```

Notes:
- The app reads env via Pydantic settings (`app/config.py`), mapping to `settings` fields.
- For local dev, `.env` is loaded automatically by the app.

## Run with Docker
```bash
cd /Users/naveensabariguru/BTC/chatbot
docker compose up --build
```
Service: `http://localhost:8000` (health endpoint is not implemented; use `/webhook` for verification).

## Webhook setup in 360dialog
- Verification (GET):
  - URL: `https://<public-domain>/webhook`
  - Params: `hub.mode=subscribe&hub.verify_token=change-me&hub.challenge=123`
  - The service will echo `hub.challenge` if the token matches `VERIFY_TOKEN`.
- Messages (POST): `https://<public-domain>/webhook`

Expose your local server via ngrok or cloudflared.

## Local testing
Send a sample message event:
```bash
curl -X POST http://localhost:8000/webhook \
  -H 'Content-Type: application/json' \
  -d '{
    "contacts": [{"wa_id": "14155550123"}],
    "messages": [{"from": "14155550123", "type": "text", "text": {"body": "Hi"}}],
    "entry": [{"changes": [{"value": {"messages": [{"from": "14155550123", "type": "text", "text": {"body": "Hi"}}]}}]}]
  }'
```

## Database
- On startup, the app will create a `messages` table if not present (`app/db.py`).
- It stores `sender`, `message`, and a `created_at` timestamp for each incoming text.

## Code overview
- `app/main.py`: FastAPI app, GET `/webhook` verification, POST `/webhook` receipt, echo and DB insert.
- `app/config.py`: Pydantic settings reading env keys.
- `app/db.py`: psycopg2 connection and init.
- `Dockerfile`, `docker-compose.yml`: containerization and Postgres service.

## Production notes
- Use a production ASGI server command (remove `--reload`).
- Use a secrets manager for env vars.
- Add request validation and signature verification if needed.
- Consider async HTTP client and DB access for higher throughput.
