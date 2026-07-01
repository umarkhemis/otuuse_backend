# Kabale Transport Platform - Backend

AI-agent-powered ride-hailing and delivery platform for Kabale, Uganda.

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI |
| Database | PostgreSQL 16 + PostGIS |
| Cache | Redis 7 |
| Task Queue | Celery + Redis |
| AI Agent | Groq (development) / Anthropic Claude (production) |
| Geocoding | Nominatim (OpenStreetMap) |
| Routing | OpenRouteService |
| Push Notifications | Firebase Cloud Messaging |
| Payments | PesaPal (MTN MoMo + Airtel Money) |
| SMS (OTP) | Africa's Talking |

---

## Prerequisites

- Docker and Docker Compose
- A Groq API key (free at console.groq.com)
- A Firebase project with a service account JSON file
- An OpenRouteService API key (free at openrouteservice.org)

---

## Local Development Setup

### 1. Clone and configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in at minimum:
- `APP_SECRET_KEY` - any random 32+ character string
- `GROQ_API_KEY` - from console.groq.com
- `ORS_API_KEY` - from openrouteservice.org
- `AFRICASTALKING_USERNAME` and `AFRICASTALKING_API_KEY`

### 2. Add Firebase credentials

Place your Firebase service account JSON file at:
```
firebase-service-account.json
```

### 3. Start the full environment

```bash
make dev
```

This starts PostgreSQL + PostGIS, Redis, the FastAPI server,
Celery worker, Celery Beat, and Flower task monitor.

### 4. Run database migrations

```bash
make migrate
```

### 5. Verify everything is running

- API: http://localhost:8000
- API Docs (Swagger): http://localhost:8000/docs
- Celery Flower: http://localhost:5555

---

## Running Tests

```bash
make test           # all tests
make test-unit      # unit tests only (no external dependencies)
make test-integration  # integration tests (requires running DB)
```

---

## Environment Variables

See `.env.example` for the full list with descriptions.

Key variables:

| Variable | Description |
|---|---|
| `LLM_PROVIDER` | `groq` for development, `anthropic` for production |
| `GROQ_API_KEY` | Groq API key |
| `GROQ_MODEL` | Default: `llama-3.1-8b-instant` |
| `ANTHROPIC_API_KEY` | For production switch to Claude |
| `PESAPAL_BASE_URL` | Use `cybqa.pesapal.com` for sandbox |
| `APP_ENV` | `development` / `staging` / `production` |
| `STORAGE_PROVIDER` | `local` for development, `s3` for production (driver documents) |

---

## Switching from Groq to Anthropic (Production)

In `.env`:
```
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-your-key-here
LLM_MODEL=claude-haiku-4-5-20251001
```

No code changes needed. The `LLMClient` routes automatically.

---

## Project Structure

```
kabale_transport/
    app/
        api/
            routes/         - FastAPI route handlers (auth, routes, admin, rides)
            dependencies.py - Auth dependencies
        core/
            config.py       - All settings (env vars)
            security.py     - JWT, OTP, PIN utilities
            logging.py      - Structured logging
        db/
            session.py      - Async SQLAlchemy session
        models/
            models.py       - All ORM models (PostGIS geometry)
        services/
            agent/
                agent.py    - AI agent orchestration
                llm_client.py - Groq/Anthropic unified client
                system_prompt.py - Agent prompt builder
                context.py  - Conversation window
            dispatch.py     - Driver matching and GPS state machine
            geocoding.py    - Nominatim geocoding
            routing.py      - ORS distance/fare calculation
            payment.py      - PesaPal integration
            notifications.py - FCM push notifications
            sms.py          - Africa's Talking SMS
            storage.py      - Driver document storage (local disk / S3-compatible)
            cache.py        - Redis utilities
            crud.py         - Database operations
        tasks/
            dispatch_tasks.py - Celery async tasks
        main.py             - Application entry point
    tests/
        unit/               - No-dependency unit tests
        integration/        - Full stack integration tests
        conftest.py         - Shared fixtures
    alembic/
        versions/           - Migration files
        env.py
    Dockerfile
    docker-compose.yml
    Makefile
    requirements.txt
```

---

## API Endpoints Summary

| Method | Path | Description | Auth |
|---|---|---|---|
| POST | `/api/v1/auth/request-otp` | Request OTP | None |
| POST | `/api/v1/auth/verify-otp` | Verify OTP, get tokens | None |
| POST | `/api/v1/auth/refresh` | Exchange refresh token for a new pair | None |
| POST | `/api/v1/auth/logout` | Revoke refresh token, deactivate push token | Any |
| POST | `/api/v1/auth/push-token` | Register/refresh FCM device token | Any |
| DELETE | `/api/v1/auth/push-token` | Deactivate FCM device token | Any |
| POST | `/api/v1/chat/message` | Send message to AI agent | Passenger |
| POST | `/api/v1/chat/confirm-ride` | Confirm fare and dispatch | Passenger |
| GET | `/api/v1/rides/active` | Caller's current in-progress ride | Any |
| GET | `/api/v1/rides/{id}` | Ride detail + live driver location | Passenger/Driver on ride |
| POST | `/api/v1/rides/{id}/rate` | Rate the other party post-ride | Passenger/Driver on ride |
| POST | `/api/v1/driver/location` | GPS update | Driver |
| POST | `/api/v1/driver/availability` | Go online/offline | Driver |
| POST | `/api/v1/driver/ride/{id}/action` | Accept/decline/complete ride | Driver |
| GET | `/api/v1/driver/earnings` | Earnings summary | Driver |
| POST | `/api/v1/driver/documents` | Upload verification document | Driver |
| POST | `/api/v1/payments/topup/initiate` | Start wallet top-up | Passenger |
| GET | `/api/v1/payments/pesapal/ipn` | PesaPal IPN callback | None |
| GET | `/api/v1/payments/wallet/balance` | Check wallet | Any |
| POST | `/api/v1/payments/withdraw` | Request withdrawal | Driver |
| GET | `/api/v1/payments/transactions` | Transaction history | Any |
| POST | `/api/v1/admin/drivers/onboard` | Onboard new driver | Admin |
| GET | `/api/v1/admin/drivers` | List all drivers | Admin |
| PATCH | `/api/v1/admin/drivers/{id}/suspend` | Suspend driver | Admin |
| PATCH | `/api/v1/admin/drivers/{id}/reinstate` | Reinstate driver | Admin |
| POST | `/api/v1/admin/drivers/{id}/renew-subscription` | Renew subscription | Admin |
| GET | `/api/v1/admin/drivers/{id}/documents` | View driver verification documents | Admin |
| PATCH | `/api/v1/admin/drivers/{id}/verify-documents` | Approve driver documents | Admin |
| GET | `/api/v1/admin/deliveries` | List delivery requests | Admin |
| POST | `/api/v1/admin/deliveries/{id}/reply` | Reply to delivery | Admin |
| GET | `/api/v1/admin/dashboard` | Platform stats | Admin |
| GET | `/api/v1/admin/rides` | List rides | Admin |
| GET | `/api/v1/admin/rides/{id}/trail` | GPS trail for dispute | Admin |
