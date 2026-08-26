<p align="center">
  <img src="branding/lunartech-banner.png" alt="LUNARTECH" width="100%">
</p>

<h1 align="center">Dark Phoenix</h1>

<p align="center">
  <strong>AI-powered podcast video clipper</strong><br>
  A <a href="https://lunartech.ai/">LUNARTECH</a> Labs project<br>
  Created by <a href="https://www.linkedin.com/in/vahe-aslanyan/">Vahe Aslanyan</a>
</p>

Upload a long-form podcast video, the backend runs active-speaker detection +
transcription + LLM-driven moment selection, and the frontend serves the
resulting short-form clips back to the user.

## Architecture

```
┌──────────────────────────┐        ┌──────────────────────────┐
│  ai-podcast-clipper-     │        │  ai-podcast-clipper-     │
│  frontend (Next.js 15)   │        │  backend (Modal/Python)  │
│                          │        │                          │
│  • Auth.js + Discord     │ HTTPS  │  • TalkNet ASD           │
│  • Prisma → Supabase PG  │ ─────▶ │  • WhisperX transcribe   │
│  • Stripe credit packs   │        │  • Gemini moment picker  │
│  • Inngest queue         │        │  • ffmpeg clip render    │
└──────────────────────────┘        └──────────────────────────┘
            │                                   │
            ▼                                   ▼
        AWS S3 ◀─── shared bucket: source uploads + rendered clips ──▶
```

The frontend authenticates the user, takes payment for credits, uploads the
source video to S3, then calls a Modal HTTPS endpoint. The Modal worker pulls
the source from S3, processes it, and writes clips back to S3 for the frontend
to surface.

## Repository layout

```
dark-phoenix/
├── .env                           ← single source of truth (gitignored)
├── .env.example                   template — copy to .env and fill in
├── ai-podcast-clipper-frontend/   Next.js (T3) app — UI, auth, billing
│   ├── src/                        app code (App Router)
│   ├── prisma/                     schema + migrations
│   └── next.config.js              loads ../.env via @next/env
└── ai-podcast-clipper-backend/    Python Modal app — GPU video processing
    ├── main.py                     Modal entrypoint
    ├── asd/                        TalkNet active-speaker detection
    ├── setup_modal_secret.py       loads ../.env into Modal Secret
    └── requirements.txt
```

## Prerequisites

- Node.js 20+ and npm
- Python 3.11+ (for the backend / Modal)
- A [Modal](https://modal.com) account with the CLI authenticated (`modal token new`)
- An AWS account with an S3 bucket
- A Supabase project (Postgres)
- A Stripe account (test mode is fine for development)
- A Discord application for OAuth
- A Google AI Studio API key for Gemini

## Setup

### 1. Configure environment variables

There is **one** environment file for the whole project, at the repo root:
[.env](.env). The frontend's
[next.config.js](ai-podcast-clipper-frontend/next.config.js) loads it via
`@next/env`'s `loadEnvConfig('..')`, and the backend's
[setup_modal_secret.py](ai-podcast-clipper-backend/setup_modal_secret.py)
reads the same file via `python-dotenv`.

```bash
cp .env.example .env
# then open .env and fill in real values
```

The `.env` file is gitignored. Never commit it. If you ever need to update
the schema (add/remove variables), edit
[src/env.js](ai-podcast-clipper-frontend/src/env.js) so the runtime
validation stays in sync.

### 2. Frontend

```bash
cd ai-podcast-clipper-frontend
npm install
npx prisma db push       # apply schema to your Supabase database
npm run dev              # http://localhost:3000
```

### 3. Backend (Modal)

```bash
cd ai-podcast-clipper-backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Push your env values into a Modal Secret named "ai-podcast-clipper-secret"
python setup_modal_secret.py

# Deploy the worker
modal deploy main.py
```

`modal deploy` prints the public endpoint URL. Paste that into
`PROCESS_VIDEO_ENDPOINT` in `.env` and choose a value for
`PROCESS_VIDEO_ENDPOINT_AUTH` (the frontend sends it as a bearer token; the
backend checks it).

### 4. Stripe webhook (optional, for credit purchases)

For local development:

```bash
stripe listen --forward-to localhost:3000/api/stripe/webhook
```

Copy the `whsec_…` secret it prints into `STRIPE_WEBHOOK_SECRET`.

## Common scripts

| Where | Command | What it does |
|-------|---------|--------------|
| frontend | `npm run dev` | Next.js dev server with Turbo |
| frontend | `npm run build` | Production build |
| frontend | `npm run check` | Lint + typecheck |
| frontend | `npm run db:studio` | Prisma Studio (DB GUI) |
| frontend | `npm run db:push` | Push schema to Postgres |
| backend  | `modal deploy main.py` | Deploy/redeploy the worker |
| backend  | `modal run main.py` | Run a one-off invocation |

## Security

- `.env` is gitignored. `.env.example` is the only env file that should ever
  be committed, and it must contain placeholders only.
- All secrets — AWS keys, Stripe keys, database password, NextAuth secret,
  Modal endpoint token, Gemini key — live in a single `.env`. Rotate them by
  generating new values in their respective consoles and pasting back.
- The Modal endpoint is protected by a shared bearer token
  (`PROCESS_VIDEO_ENDPOINT_AUTH`); rotate it any time the frontend or
  backend is redeployed by an untrusted party.

## License

See [LICENSE.MD](LICENSE.MD).
