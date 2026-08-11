# iScale Console — frontend

React + Vite. A two-screen internal tool: sign in, then read and take over
WhatsApp conversations handled by the bot.

Deployed **separately** from the FastAPI backend, which is why auth uses a
bearer token rather than the httpOnly cookie the same-origin console uses —
Safari and Brave block third-party cookies outright, so a cookie would work in
Chrome and silently fail elsewhere.

## Develop

```bash
npm install
npm run dev            # http://localhost:5173
```

`vite.config.ts` proxies `/api` to `http://127.0.0.1:8000`, so development is
same-origin and needs no CORS setup. Point it elsewhere with `VITE_DEV_API`.

Run the backend alongside it:

```bash
cd ..
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

## Build

```bash
npm run build          # -> dist/
npm run typecheck
```

## Deploy

Set **`VITE_API_BASE`** to the backend URL. It is baked into the bundle at build
time, so changing it needs a rebuild — it is not read at runtime.

```
VITE_API_BASE=https://iscale-whatsapp-chatbot.onrender.com
```

Then add the console's own URL to the backend's allow-list, or every request is
blocked by CORS:

```
CONSOLE_ALLOWED_ORIGINS=https://your-console.vercel.app
```

Both sides must agree. A missing entry there is the most likely reason a freshly
deployed console cannot log in.

### Vercel
Import the repo, set the root directory to `frontend`, add `VITE_API_BASE`.
`vercel.json` handles SPA routing and the security headers.

### Render static site
Root `frontend`, build `npm install && npm run build`, publish `dist`, and add a
rewrite from `/*` to `/index.html`.

## Notes

- The bundle is ~51 kB gzipped and deliberately not code-split; a two-screen app
  behind a login gains nothing from lazy chunks.
- Live updates come over a WebSocket carrying only a nudge, never message
  content. Polling continues as a fallback so a dropped socket costs latency
  rather than a dead page.
- The token is in `localStorage`. That is more XSS-exposed than an httpOnly
  cookie — the honest cost of hosting the frontend on another domain. React
  escapes rendered content by default and nothing here uses
  `dangerouslySetInnerHTML`.
