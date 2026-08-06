# iScale WhatsApp AI Receptionist

An AI receptionist for iScale on WhatsApp. It answers questions from a curated
knowledge base, helps people choose a course, separates pre-sales enquiries from
enrolled-student support, captures a callback request, and hands the lead to a
human counselor.

It is deliberately **not** a closer. Fees, offers, batch dates and any
commitment are always routed to a person.

---

## Contents

- [How it works](#how-it-works)
- [Project structure](#project-structure)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [The knowledge base](#the-knowledge-base)
- [WhatsApp setup](#whatsapp-setup)
- [Google Sheets setup](#google-sheets-setup)
- [Conversation flow](#conversation-flow)
- [API reference](#api-reference)
- [Database](#database)
- [Testing](#testing)
- [Deployment](#deployment)
- [Extending it](#extending-it)

---

## How it works

```
WhatsApp  ──▶  /webhook  ──▶  ConversationService  ──▶  ConversationMachine
                  │                    │                        │
           signature check      persist transcript        state handlers
           fast 200 + bg task   load history                    │
                                                    ┌───────────┴───────────┐
                                                    ▼                       ▼
                                              AnswerService            LeadService
                                                    │                       │
                                          retrieve ──▶ Groq          PostgreSQL ──▶ Sheets
```

Three ideas carry most of the design:

**Flow lives in a state machine, not in the model.** The conversation's position
is a column in PostgreSQL. The model is asked to write prose; it is never asked
what happens next. A lead is created because the machine reached `ASK_REMARKS`
with a validated time slot, not because a model decided it had enough
information.

**The model only ever sees retrieved knowledge.** Questions go through a BM25
retriever that selects a handful of relevant snippets from the JSON knowledge
base. The full knowledge base is never sent. Courses are indexed field by field,
so "how long is it?" retrieves the duration, not the entire syllabus.

**Scheduling is rules, not inference.** Callback times are parsed and validated
deterministically against business hours and closed days. A model cannot
hallucinate a slot on a day the office is shut.

---

## Project structure

```
app/
├── main.py                 FastAPI factory, middleware, exception handlers
├── container.py            Composition root - every concrete class is chosen here
├── core/                   config, logging, exception hierarchy
├── domain/                 enums, channel-agnostic message value objects
├── db/
│   ├── base.py             declarative base, portable JSON column
│   ├── session.py          async engine + session scopes
│   └── models/             User, Conversation, Message, Lead
├── repositories/           data access - services never write SQL
├── schemas/                Pydantic: webhook payloads, API responses
├── bot/
│   ├── machine.py          dispatcher + global commands
│   ├── context.py          per-turn context and injected dependencies
│   ├── copy.py             every user-facing string and menu
│   ├── intents.py          deterministic yes/no, escalation, option matching
│   └── handlers/           one handler per state
├── services/
│   ├── knowledge/          JSON loader + BM25 retriever
│   ├── llm/                Groq client, prompt builder, answer service
│   ├── whatsapp/           Cloud API client, parser, signature check, allowlist
│   ├── crm/                LeadSink protocol, Google Sheets, null sink
│   ├── scheduling/         callback time parsing and validation
│   ├── conversation_service.py   per-message orchestration
│   └── lead_service.py     lead creation and CRM sync
└── api/v1/                 webhook, health, leads, simulate

knowledge/                  courses, faqs, policies, company, placements (JSON)
alembic/                    migrations
tests/                      185 tests, no network required
```

Dependencies point inwards. `bot/` depends on protocols
(`LLMClient`, `KnowledgeRetriever`, `LeadSink`, `MessagingClient`), never on
Groq, Google or Meta. `container.py` is the only module that names a concrete
integration.

---

## Quick start

### Option A — Docker (recommended)

```bash
cp .env.example .env
# Fill in GROQ_API_KEY, WHATSAPP_ALLOWED_NUMBERS and the WhatsApp values, then:
docker compose up --build
```

Postgres starts, migrations run automatically, the API listens on
<http://localhost:8000>. Docs at <http://localhost:8000/docs>.

### Option B — local Python

Requires Python 3.11+ and a running PostgreSQL.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

cp .env.example .env               # then edit it

createdb iscale                    # or use an existing database
alembic upgrade head

uvicorn app.main:app --reload
```

### Try it without WhatsApp

Set `WHATSAPP_ENABLED=false` and drive the real state machine over HTTP:

```bash
curl -X POST localhost:8000/api/v1/simulate \
  -H 'Content-Type: application/json' \
  -d '{"phone":"919876543210","text":"hi"}'
```

```json
{
  "state": "MAIN_MENU",
  "replies": [
    {
      "text": "Welcome to iScale! 👋\n\nHow can I help you today?",
      "options": [
        { "id": "menu:courses",    "title": "Explore Courses" },
        { "id": "menu:enrolled",   "title": "Already Enrolled" },
        { "id": "menu:counselor",  "title": "Talk to Counselor" },
        { "id": "menu:general",    "title": "General Question" }
      ]
    }
  ],
  "lead_id": null
}
```

Continue by passing `reply_id` to simulate a tap:

```bash
curl -X POST localhost:8000/api/v1/simulate \
  -H 'Content-Type: application/json' \
  -d '{"phone":"919876543210","reply_id":"menu:courses"}'
```

---

## Configuration

All settings come from the environment (or `.env`) via Pydantic Settings, and
are validated at startup. See [`.env.example`](.env.example) for the full list.

The ones that matter most:

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://…` | Must use the async driver. A `postgresql://` URL is upgraded automatically. |
| `GROQ_API_KEY` | — | Required for answers. Menus and lead capture still work without it. |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Any Groq chat model. |
| `WHATSAPP_ALLOWLIST_ENABLED` | `true` | Development guard. **Leave on until launch.** See [Development safety](#development-safety-the-allowlist). |
| `WHATSAPP_ALLOWED_NUMBERS` | — | Comma-separated numbers the bot may talk to. Empty + enabled = replies to nobody. |
| `WHATSAPP_ENABLED` | `true` | `false` logs replies instead of sending them. |
| `WHATSAPP_VERIFY_TOKEN` | — | You invent this; paste the same value into Meta's webhook screen. |
| `WHATSAPP_APP_SECRET` | — | Enables `X-Hub-Signature-256` verification. **Required in production** — the webhook refuses unverified requests when `ENVIRONMENT=production`. |
| `GOOGLE_SHEETS_ENABLED` | `false` | When off, leads live in PostgreSQL only. |
| `BUSINESS_OPEN_TIME` / `BUSINESS_CLOSE_TIME` | `11:00` / `19:00` | Callback window. |
| `BUSINESS_CLOSED_WEEKDAYS` | `friday` | Comma-separated names or numbers. |
| `BUSINESS_TIMEZONE` | `Asia/Kolkata` | Any IANA zone. |
| `QNA_NUDGE_THRESHOLD` | `3` | Answered questions before the bot offers a callback. |
| `CALLBACK_MIN_LEAD_MINUTES` | `30` | Rejects slots that are too imminent. |

---

## The knowledge base

Five JSON files in `knowledge/`. Ships with working sample data — **replace the
contents with real iScale information before going live.** The bot can only say
what is in here.

| File | Shape |
|---|---|
| `courses.json` | `{"courses": [ … ]}` |
| `faqs.json` | `{"faqs": [ … ]}` |
| `policies.json` | `{"policies": [ … ]}` |
| `company.json` | `{"company": { … }}` |
| `placements.json` | `{"placements": { … }}` |

### Course entry

Only `name` is required. Every other field becomes its own retrievable snippet
when present, so partial entries are fine.

```json
{
  "slug": "data-analytics",
  "name": "Data Analytics",
  "short_description": "Turn raw data into business decisions.",
  "duration": "4 months",
  "effort": "8-10 hours per week",
  "mode": "Online live classes",
  "level": "Beginner friendly",
  "eligibility": "Any graduate. No prior coding experience required.",
  "fees": { "display": "Confirmed by a counselor on the call" },
  "tools": ["Excel", "SQL", "Power BI"],
  "curriculum": [{ "module": "SQL for analysts", "topics": ["Joins"] }],
  "projects": ["Retail sales dashboard"],
  "batches": { "weekend_available": true },
  "certification": "iScale certificate on completing the capstone.",
  "career_outcomes": ["Data Analyst"],
  "keywords": ["analytics", "analyst", "dashboard"]
}
```

`keywords` are how a typed message ("i want power bi") maps onto a course.
Adding a course to `courses.json` adds it to the WhatsApp menu — no code change.

### FAQ entry

```json
{
  "id": "faq-weekend",
  "question": "Do you have weekend batches?",
  "answer": "Yes, weekend batches are available on most programs.",
  "audience": "all",
  "tags": ["weekend", "batch", "timing"]
}
```

`audience` is `all`, `pre_sales` or `post_sales`. It is enforced at retrieval
time, so an enrolled student's support question never pulls in sales copy, and a
prospect never sees student-portal instructions.

`tags` are weighted heavily in ranking — they are the cheapest way to make a
specific question land on a specific answer.

Files are read once at startup. Edit and restart (with Docker, the `knowledge/`
directory is mounted, so a container restart is enough — no rebuild).

---

## Development safety — the allowlist

The bot's webhook receives **every** message the connected number gets. During
development that includes real customers. The allowlist is what stops them
being answered by a half-built bot.

```bash
WHATSAPP_ALLOWLIST_ENABLED=true
WHATSAPP_ALLOWED_NUMBERS=919876543210     # your own number, country code, digits only
```

It is enforced in two independent places:

| Layer | Where | Effect |
|---|---|---|
| Inbound | `api/v1/webhook.py` | A message from an unlisted number is dropped before any DB write, LLM call, read receipt or reply. No trace, no blue ticks. |
| Outbound | `GuardedMessagingClient` | A send to an unlisted number is refused even if a bug routes one there. |

One layer would cover the normal path. Two mean a mistake in the first cannot
put a message in front of a stranger.

**The defaults are fail-closed.** Enabled with an empty list means the bot
replies to *nobody* — that is deliberate. Being ignored during development is
recoverable; messaging a real customer is not. Confirm the posture at any time:

```bash
curl localhost:8000/api/v1/simulate/allowlist
```

The startup banner logs the same thing, loudly, including a warning when the
guard is off.

> Turn the allowlist off (`WHATSAPP_ALLOWLIST_ENABLED=false`) only when you are
> genuinely ready for the public to reach the bot.

---

## WhatsApp setup

> **Read this first if the number is a live business number.**
> A WhatsApp Business number has exactly **one** webhook URL. Pointing it at
> your laptop means *you receive the traffic other systems were receiving* —
> they stop working for as long as your tunnel is up. The allowlist protects
> other *people*; it cannot protect other *systems* on the same number.
>
> So: **develop against a test number, not the business number.** Meta gives
> every developer app a free test number that can message up to 5 recipients you
> nominate. Nothing about the code changes — only which `WHATSAPP_PHONE_NUMBER_ID`
> and token you paste in. Swap in the production number at launch.

1. Create a Meta app with the **WhatsApp** product at
   <https://developers.facebook.com>.
2. Under **API Setup**, use the provided **test number**. Add your own number
   under *To* — Meta will send a verification code.
3. Note the **Phone number ID** and generate a permanent **access token**.
4. Expose your local server: `ngrok http 8000`.
5. In **WhatsApp → Configuration → Webhook**, set:
   - Callback URL: `https://<your-host>/api/v1/webhook`
   - Verify token: the same string as `WHATSAPP_VERIFY_TOKEN`
6. Subscribe to the **`messages`** field.
7. Copy the **App Secret** into `WHATSAPP_APP_SECRET`.

### If the number is managed by a BSP (MSG91, Gupshup, Twilio…)

A BSP owns the Meta integration for that number, and the webhook is configured
in *their* panel, not Meta's. Two consequences:

- You will not get an App Secret from Meta, so `X-Hub-Signature-256`
  verification does not apply. Leave `WHATSAPP_APP_SECRET` empty in development;
  before production, put the app behind an alternative check (a shared secret in
  the callback URL, or IP allowlisting the BSP).
- The inbound JSON is the BSP's shape, not Meta's. `services/whatsapp/parser.py`
  and `client.py` are the only two modules that would need a BSP variant —
  everything above them speaks `InboundMessage` / `OutboundMessage`.

The clean path is to ask the BSP for a **separate sandbox/test number** and
leave the production number's webhook untouched until launch.

### Templates

You need a **template** only to *start* a conversation with someone who has not
messaged you in the last 24 hours. This bot is purely reactive — it always
replies inside that 24-hour service window — so **no template is required** for
anything it currently does. You would need one only if you later add outbound
reminders or follow-ups the user did not initiate.

Two things the implementation handles that trip people up:

- **Verification** returns the raw `hub.challenge` as `text/plain`, not JSON.
- **Retries.** Meta redelivers until it gets a 200, so the webhook acknowledges
  immediately and processes in a background task. Every message is de-duplicated
  on its WhatsApp id via a unique index, so a redelivery never produces a second
  reply.

---

## Google Sheets setup

1. Google Cloud Console → create a **service account** → download the JSON key.
2. Enable the **Google Sheets API** for the project.
3. Create a spreadsheet and **share it as Editor with the service account's
   email address** (the step everyone forgets).
4. Configure:

```env
GOOGLE_SHEETS_ENABLED=true
GOOGLE_SHEETS_SPREADSHEET_ID=<the id from the sheet URL>
GOOGLE_SHEETS_WORKSHEET_NAME=Leads
GOOGLE_SERVICE_ACCOUNT_FILE=./secrets/service-account.json
# or, for PaaS hosts where files are awkward:
# GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account",...}
```

The header row is written automatically if the sheet is empty:

| Date | Phone | Name | Lead Type | Interested Course | Preferred Callback Time | Remarks | Status |
|---|---|---|---|---|---|---|---|

Sheets is a **secondary** store. The lead is committed to PostgreSQL first and
pushed afterwards; a failed push is recorded on the row (`sync_status`,
`sync_error`) and never costs you the lead. Retry with:

```bash
curl -X POST localhost:8000/api/v1/leads/sync-pending
```

---

## Conversation flow

```
                          START
                            │
                       MAIN_MENU
        ┌───────────────┬───┴────────┬──────────────────┐
        ▼               ▼            ▼                  ▼
 COURSE_SELECTION   POST_SALES   ASK_NAME ◀───┐   GENERAL_QNA
        │               │                     │        │
        ▼               ▼                     │        │
   COURSE_QNA     SUPPORT_QUERY               │        │
        │               │                     │        │
        │               ▼                     │        │
        │        SUPPORT_CALLBACK ────────────┤        │
        │                                     │        │
        └──────────▶ ASK_CALLBACK ────────────┘◀───────┘
                            │
                            ▼
                   ASK_CALLBACK_TIME
                            │
                            ▼
                      ASK_REMARKS
                            │
                            ▼
                     LEAD_CREATED ──▶ conversation closed
```

Notes on behaviour:

- **The nudge fires once.** After `QNA_NUDGE_THRESHOLD` answered questions the
  bot offers a callback. Declining returns to Q&A and it does not ask again.
- **Escalation works from anywhere.** "talk to a counselor", "call me" and
  similar jump straight into lead capture — deterministically, so it still works
  during a model outage.
- **Global commands are suppressed mid-form.** "menu" resets the flow, except
  while capturing a name, time or note, where being thrown out halfway is worse
  than the occasional missed command.
- **A known contact skips the name question.**
- **Non-answers are answered.** Asking a new question at a yes/no prompt gets
  the question answered rather than a demand for a yes or no.
- **A finished thread is retired,** and the next message opens a fresh
  conversation — the transcript stays intact for auditing.

### Callback time parsing

Understood: `tomorrow 4pm`, `today 5:30 pm`, `monday at 11:30 am`,
`day after tomorrow morning`, `12/08 3 pm`, `15 aug 2:30pm`, `at 4`, `evening`,
`asap`.

Rejected with a reason and three concrete suggestions: past times, slots inside
the lead-time window, closed days, hours outside the calling window, dates
beyond the horizon, and anything unparseable.

`at 4` resolves to 4 PM rather than 4 AM because the parser tries both readings
and keeps the one inside business hours.

---

## API reference

All routes are under `API_PREFIX` (default `/api/v1`).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness. Touches nothing. |
| `GET` | `/health/ready` | Readiness. Reports DB, knowledge base, Groq, sink. 503 if the DB is down. |
| `GET` | `/webhook` | Meta verification handshake. |
| `POST` | `/webhook` | Inbound events. Signature-verified. |
| `GET` | `/leads` | Recent leads (`limit`, `offset`). |
| `GET` | `/leads/{id}` | One lead. |
| `POST` | `/leads/sync-pending` | Retry failed CRM pushes. |
| `POST` | `/simulate` | Send a message as a test user. Disabled in production. |
| `GET` | `/simulate/outbox` | What the logging client "sent". |
| `GET` | `/simulate/allowlist` | Who the bot may currently reply to. Check this before going live. |

> The lead endpoints are unauthenticated. Keep them behind your ingress, or add
> an API-key dependency, before exposing them.

---

## Database

Four tables. `Conversation.context` is a JSONB scratch column holding the
in-flight flow data (pending name, parsed slot, question count), which means new
flow steps need no migration.

```
users ──┬──< conversations ──< messages
        └──< leads >───────────┘
```

```bash
alembic upgrade head                          # apply
alembic revision --autogenerate -m "message"  # after changing a model
alembic downgrade -1                          # roll back one
```

Enums are stored as `VARCHAR`, not native Postgres enums, so adding a
conversation state does not require an `ALTER TYPE`.

---

## Testing

```bash
pytest                       # 167 tests, ~25s, no network
pytest --cov=app             # with coverage
ruff check app tests
mypy app
```

Tests run the real state machine, repositories and orchestrator against a
temporary SQLite database. Only Groq and WhatsApp are faked. The callback
validator is pinned to a fixed Wednesday so the suite does not pass or fail
depending on the day it runs.

Covered: full pre-sales and post-sales lead capture, the nudge threshold and its
once-only rule, callback time parsing and rejection, retrieval ranking and
audience filtering, prompt grounding, webhook signature verification, webhook
de-duplication, model-outage fallback, and CRM sync failure handling.

---

## Deployment

```bash
docker compose up --build -d
docker compose logs -f api
```

Before going live:

- `ENVIRONMENT=production` — disables `/docs`, `/simulate`, and refuses webhooks
  that cannot be signature-verified.
- `LOG_JSON=true` — single-line structured logs with a correlation id that
  follows one WhatsApp message across the webhook, the state machine and the model.
- Set `WHATSAPP_APP_SECRET`.
- Put real content in `knowledge/`.
- Run migrations as a one-shot job (`RUN_MIGRATIONS=false` on the app
  containers) if you run more than one replica, so concurrent boots do not race.
- Add authentication to `/leads`.

---

## Extending it

The seams are already in place.

| Want to add | Do this |
|---|---|
| **Redis** for conversation state | Implement `ConversationRepository`'s interface against Redis, swap it in `ConversationService`. The bot never touches SQLAlchemy. |
| **HubSpot** or another CRM | Implement `LeadSink` (`push_lead`, `enabled`, `name`), swap it in `container.py`. Or fan out to several sinks. |
| **Vector search / RAG** | Implement `KnowledgeRetriever.retrieve`, change `build_retriever`. Nothing above it changes. |
| **A different LLM** | Implement `LLMClient` (`generate`, `health_check`). |
| **A new conversation step** | Add a `ConversationState`, write a handler, register it in `bot/handlers/__init__.py`. The dispatcher does not change. |
| **A new course** | Add an entry to `courses.json`. |
| **Voice notes / images** | `MessageKind.UNSUPPORTED` is where they land today — add a transcription step in the parser. |
| **Background workers** | `LeadSyncService.retry_pending()` is already idempotent and ready to schedule. |
| **Admin dashboard** | Build on `/leads`; the transcript in `messages` gives you full conversation replay. |
