# KFC Voice Agent — Backend API

A Django REST API backend powering a KFC voice AI ordering and appointment system. Integrates with ElevenLabs for voice AI (browser and outbound phone calls), Google Calendar for appointment scheduling, and Gmail SMTP for customer email confirmations.

---

## Tech Stack

- **Framework:** Django 5.2 + Django REST Framework
- **Voice AI:** ElevenLabs Conversational AI
- **Calendar:** Google Calendar API
- **Email:** Gmail SMTP
- **Database:** SQLite (development)
- **Auth:** No authentication (open API — add before production)

---

## Project Structure

```
kfc_api/
├── kfc_api/          # Django project config (settings, root urls)
├── menu/             # Orders, menu items, voice AI calls
├── Analytics/        # Dashboard stats, ElevenLabs webhook handler
└── appointment/      # Appointment booking, scheduling, email
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

Create a `.env` file in the project root:

```env
ELEVENLABS_API_KEY=your_elevenlabs_api_key
AGENT_ID=your_elevenlabs_agent_id
ELEVENLABS_VOICE_ID=your_elevenlabs_voice_id
GROQ_API_KEY=your_groq_api_key
DEEPGRAM_API_KEY=your_deepgram_api_key
ELEVEN_LABS_WEBHOOK_SECRET=your_webhook_secret
GOOGLE_CREDENTIALS_JSON=path/to/google_credentials.json
```

### 3. Run migrations

```bash
python manage.py migrate
```

### 4. Start the server

```bash
python manage.py runserver
```

---

## API Endpoints

### Menu

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/menu/` | List all menu items |
| `POST` | `/menu/` | Add a new menu item |

---

### Orders

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/orders/` | List all orders |
| `POST` | `/orders/` | Place a new order |

**POST body:**
```json
{
  "customer_name": "John Doe",
  "phone_number": "+923001234567",
  "address": "123 Main St",
  "landmark": "Near park",
  "items": [{"name": "Zinger", "qty": 2, "price": 500}],
  "total_price": 1000
}
```

---

### Voice AI (ElevenLabs)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/voice-ai/signed-url/` | Generate signed WSS URL for browser voice chat |
| `GET` | `/voice-ai/health/` | Check ElevenLabs configuration status |

---

### Calls

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/calls/` | List all call records |
| `GET` | `/calls/status/<conversation_id>/` | Get details of a specific call (refreshed from ElevenLabs) |

**GET `/calls/` query params:**

| Param | Values |
|-------|--------|
| `status` | `initiated`, `ongoing`, `completed`, `failed` |
| `call_type` | `browser`, `outbound`, `inbound` |

---

### Analytics

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/order_stats/` | Total orders, today's orders, total revenue, menu item count |
| `GET` | `/Revenue_Performance/` | Orders and revenue per day (last 7 days) |
| `GET` | `/Sales_Distribution/` | Quantity sold per item (last 7 days) |

---

### Webhooks

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/webhooks/elevenlabs/` | ElevenLabs post-call transcription webhook (HMAC verified) |

---

### Appointments

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/appointment/all/` | List all appointments |
| `POST` | `/appointment/create/` | Book a new appointment |
| `PATCH` | `/appointment/cancel/<id>/` | Cancel an appointment |
| `GET` | `/appointment/slots/?date=YYYY-MM-DD` | Get available time slots for a date |
| `GET/POST/PATCH` | `/appointment/schedule/` | Manage weekly availability schedule |

**POST `/appointment/create/` body:**
```json
{
  "name": "Jane Smith",
  "phone": "+923001234567",
  "email": "jane@example.com",
  "date": "2026-03-10",
  "start_time": "10:00",
  "end_time": "10:30",
  "notes": "First visit"
}
```

On successful booking:
- A Google Calendar event with Meet link is created
- A confirmation email with meeting details is sent to the customer

**GET `/appointment/all/` query params:**

| Param | Values |
|-------|--------|
| `status` | `pending`, `confirmed`, `cancelled`, `completed` |
| `date` | `YYYY-MM-DD` |

---

## Environment Variables Reference

| Variable | Description |
|----------|-------------|
| `ELEVENLABS_API_KEY` | ElevenLabs API key |
| `AGENT_ID` | ElevenLabs Conversational AI agent ID |
| `ELEVENLABS_VOICE_ID` | ElevenLabs voice ID for phone-call TTS |
| `GROQ_API_KEY` | Groq API key for the LLM |
| `DEEPGRAM_API_KEY` | Deepgram API key for live transcription |
| `ELEVEN_LABS_WEBHOOK_SECRET` | Webhook signing secret for HMAC verification |

> Email (SMTP) and Google Calendar credentials are currently hardcoded in `settings.py` and `appointment/services/google_calender.py`. Move these to `.env` before deploying to production.

---

## Notes

- `DEBUG = True` and `ALLOWED_HOSTS = ['*']` — change before production
- `SECRET_KEY` in `settings.py` must be rotated before production
- No authentication is enforced on any endpoint — add token or session auth before going live
