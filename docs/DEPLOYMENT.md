# Deployment Guide

This project is deployed as two separate services:

- **Frontend** — static HTML served from **Vercel** (free, global CDN)
- **Backend** — Docker container on **Render** (free tier, auto-deploys from GitHub)

```
  git push main
     │
     ├──────────────────┐
     ▼                  ▼
┌──────────┐     ┌──────────────┐
│  Render  │     │    Vercel    │
│ (backend)│     │  (frontend)  │
│ Dockerfile│    │  index.html  │
└────┬─────┘     └──────┬───────┘
     │                  │
     │    HTTPS         │
     │ <────────────────┘
     │  POST/GET /api/v1/*
     │
     ▼
┌────────────────────────┐
│  Model Providers        │
│  - OpenAI (gpt-5.2)     │
│  - Vision (gpt-4o)      │
│  - Claude Opus 4.6      │
└────────────────────────┘
```

**How it works:** You push code to GitHub → Render automatically builds the Docker image and deploys it. Vercel does the same for the frontend. No Docker or CLI needed on your machine.

---

## Prerequisites

| Tool | Purpose |
|------|---------|
| GitHub account | Code hosting, triggers auto-deploy on both Render and Vercel |
| Render account | Backend hosting ([render.com](https://render.com), free tier works) |
| Vercel account | Frontend hosting ([vercel.com](https://vercel.com), free tier works) |

No Docker, CLI tools, or other software needed on your local machine.

---

## Step 1 — Push your code to GitHub

```bash
git add -A
git commit -m "prepare for deployment"
git push origin main
```

> **Security:** The `.env` file is in `.gitignore` and `.dockerignore` — your API keys will NOT be uploaded to GitHub or baked into the Docker image.

---

## Step 2 — Deploy the Backend on Render

### 2.1 Create a new Web Service

1. Go to [render.com/new](https://dashboard.render.com/select-repo?type=web) and sign in
2. Click **New Web Service**
3. Connect your GitHub account and select your `web-crawler` repository
4. Configure:

   | Setting | Value |
   |---------|-------|
   | **Name** | `web-crawler-api` |
   | **Region** | Pick the closest to you |
   | **Branch** | `main` |
   | **Runtime** | `Docker` (Render auto-detects the Dockerfile) |
   | **Instance Type** | `Free` |

### 2.2 Set environment variables

Scroll down to **Environment Variables** and add each:

| Key | Value |
|-----|-------|
| `AZURE_OPENAI_ENDPOINT` | `https://your-resource.openai.azure.com/` |
| `AZURE_OPENAI_API_KEY` | your key |
| `AZURE_OPENAI_API_VERSION` | `2025-04-01-preview` |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-5.2` |
| `AZURE_VISION_ENDPOINT` | your vision endpoint |
| `AZURE_VISION_API_KEY` | your vision key |
| `AZURE_VISION_DEPLOYMENT` | `gpt-4o` |
| `USE_VISION_PLANNING` | `true` |
| `AZURE_CLAUDE_ENDPOINT` | your Claude endpoint |
| `AZURE_CLAUDE_API_KEY` | your Claude key |
| `AZURE_CLAUDE_DEPLOYMENT` | `claude-opus-4-6` |
| `USE_CLAUDE_EXTRACTION` | `true` |
| `CLAUDE_FALLBACK_ONLY` | `true` |
| `CLAUDE_CIRCUIT_BREAKER_ENABLED` | `true` |
| `ALLOWED_ORIGINS` | `["*"]` (update after Vercel deploy) |
| `PLAYWRIGHT_HEADLESS` | `true` |
| `LOG_LEVEL` | `INFO` |

### 2.3 Deploy

Click **Create Web Service**. Render will:
1. Clone your repo
2. Build the Docker image (first build takes ~5-10 min)
3. Start the container
4. Give you a public URL like `https://web-crawler-api.onrender.com`

### 2.4 Verify

Open `https://<your-render-url>/health` in a browser. You should see:
```json
{"status":"ok","providers":{...}}
```

> **Note:** On the free tier, Render sleeps the service after 15 min of inactivity. The first request after sleep takes ~1-2 min to wake up (Playwright/Chromium initialization). Subsequent requests are fast.

---

## Step 3 — Deploy the Frontend on Vercel

### 3.1 Import the project

1. Go to [vercel.com/new](https://vercel.com/new)
2. Import your GitHub repository
3. Set **Root Directory** to `frontend`
4. Vercel auto-detects the build config from `frontend/vercel.json`

### 3.2 Set environment variable

In the Vercel project settings → Environment Variables, add:

| Variable | Value | Example |
|----------|-------|---------|
| `VITE_API_BASE_URL` | Your Render backend URL | `https://web-crawler-api.onrender.com` |

### 3.3 Deploy

Click **Deploy** — Vercel runs `node inject-api-url.js`, which:
1. Copies `app/frontend/index.html` to `dist/index.html`
2. Injects a `<meta name="api-base-url">` tag with your backend URL
3. The frontend JavaScript reads this tag to know where to send API requests

### 3.4 Verify

Open your Vercel URL (e.g. `https://my-crawler.vercel.app`) — you should see the Lead Scraper UI. Submit a test crawl to confirm it reaches the backend.

---

## Step 4 — Connect Frontend ↔ Backend (CORS)

After both are deployed, update the backend's CORS settings so it accepts requests from your Vercel domain.

1. Go to Render dashboard → your service → **Environment**
2. Update `ALLOWED_ORIGINS` to: `["https://my-crawler.vercel.app","http://localhost:8000"]`
3. Add `FRONTEND_URL` = `https://my-crawler.vercel.app`
4. Click **Save Changes** (triggers a redeploy)

> **Important:** `ALLOWED_ORIGINS` must be a JSON array, not comma-separated. E.g. `["https://a.com","https://b.com"]`

---

## Updating the Deployment

### Backend — just push to main

```bash
git push origin main
```

Render automatically rebuilds and redeploys when you push to `main`.

### Frontend — auto-deploys on push

Vercel also redeploys automatically when you push to `main`.

---

## Environment Variables Reference

### Required (Backend)

| Variable | Description |
|----------|-------------|
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI resource URL |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI API key |

### Optional (Backend)

| Variable | Default | Description |
|----------|---------|-------------|
| `AZURE_OPENAI_API_VERSION` | `2025-04-01-preview` | API version |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-5.2` | Model deployment name |
| `AZURE_VISION_ENDPOINT` | `""` | Vision endpoint (GPT-4o) |
| `AZURE_VISION_API_KEY` | `""` | Vision API key |
| `AZURE_VISION_API_VERSION` | `2025-04-01-preview` | Vision API version |
| `AZURE_VISION_DEPLOYMENT` | `gpt-4o` | Vision deployment name |
| `USE_VISION_PLANNING` | `true` | Enables screenshot-based planning |
| `AZURE_CLAUDE_ENDPOINT` | `""` | Azure AI Foundry Claude endpoint |
| `AZURE_CLAUDE_API_KEY` | `""` | Claude API key |
| `AZURE_CLAUDE_DEPLOYMENT` | `claude-opus-4-6` | Claude model deployment name |
| `USE_CLAUDE_EXTRACTION` | `true` | Enables Claude extraction fallback |
| `USE_SCRIPT_EXTRACTION` | `true` | Enables generated-script extraction |
| `ALLOW_GENERATED_SCRIPT_EXECUTION` | `false` | Auto-run generated scripts |
| `CLAUDE_CIRCUIT_BREAKER_ENABLED` | `true` | Disables Claude after repeated failures |
| `CLAUDE_FALLBACK_ONLY` | `true` | OpenAI-first; Claude only on fallback |
| `ALLOWED_ORIGINS` | `["*"]` | CORS allowed origins (**JSON array**) |
| `FRONTEND_URL` | `""` | Vercel frontend URL |
| `OUTPUT_DIR` | `./output` | Results storage directory |
| `PLAYWRIGHT_HEADLESS` | `true` | Run browser headless |
| `MAX_CONCURRENT_REQUESTS` | `5` | HTTP concurrency limit |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

### Frontend (Vercel)

| Variable | Description |
|----------|-------------|
| `VITE_API_BASE_URL` | Backend URL (injected at build time) |

---

## Local Development

No deployment config changes are needed for local dev:

```bash
# Backend
python -m uvicorn app.main:app --reload
# Runs at http://localhost:8000, serves frontend + API
```

When no `VITE_API_BASE_URL` is set, the frontend falls back to `window.location.origin` — which is `http://localhost:8000` when served by the backend directly.

---

## Cost Estimate

| Service | Tier | Cost |
|---------|------|------|
| Render | Free (750 hrs/month, sleeps after 15 min) | $0 |
| Vercel | Hobby (free) | $0 |
| Azure OpenAI | Pay-as-you-go | ~$0.01-0.10 per crawl |

**Total hosting cost: $0/month.** You only pay for Azure OpenAI API usage.

---

## Free Tier Limitations

| Limitation | Impact | Upgrade path |
|------------|--------|-------------|
| **512 MB RAM** | Playwright uses ~300MB, leaving limited headroom. Large scrapes may hit OOM. | Render Starter ($7/month) → 2GB RAM |
| **Sleeps after 15 min idle** | First request after sleep takes ~1-2 min (cold start). | Render Starter → always on |
| **No persistent disk** | Job history lost when service sleeps or redeploys. | Render Starter + Persistent Disk ($0.25/GB/month) |
| **Auto-deploy on push** | Every push to `main` triggers a rebuild (~5-10 min downtime). | Use a `production` branch for controlled deploys |

---

## Troubleshooting

### Render build fails

Go to Render dashboard → your service → **Events** tab to see build logs.

Common issues:
- Dockerfile syntax error
- Package install failure (check `requirements.txt`)
- Build timeout (free tier has a 30 min build limit — usually enough)

### Backend won't start / crashes on startup

Check Render dashboard → **Logs** tab.

Common issues:
- Missing `AZURE_OPENAI_ENDPOINT` or `AZURE_OPENAI_API_KEY` — crashes with `ValidationError`
- `ALLOWED_ORIGINS` set as comma-separated instead of JSON array — crashes with `ValidationError`

### `/health` returns `degraded`

`degraded` means at least one provider failed its health check.

Common causes:
- Vision provider not configured but `USE_VISION_PLANNING=true`
- Claude auth/endpoint issue
- Claude circuit breaker open after repeated runtime failures

### Frontend shows CORS errors

1. Open browser DevTools → Console
2. Verify `VITE_API_BASE_URL` was set in Vercel before deploying
3. Check that `ALLOWED_ORIGINS` on the backend includes your Vercel URL (must be a JSON array)
4. Test the backend directly: open `https://<your-render-url>/health` in a browser

### Backend is slow / timing out

On free tier, the service sleeps after 15 min of inactivity. The first request wakes it up, which takes ~1-2 min. If the frontend times out waiting:
- The frontend polls every 1-2 seconds, so it should recover once the backend is awake
- If scrapes are hitting OOM (512MB limit), reduce `MAX_CONCURRENT_REQUESTS` to `2`

### Container runs out of memory

Playwright + Chromium uses ~300MB. With 512MB on free tier, you can only run 1 browser instance. If you hit OOM:
- Set `MAX_CONCURRENT_REQUESTS=2` in Render environment variables
- Upgrade to Render Starter ($7/month) for 2GB RAM
- Use a remote browser service (`PLAYWRIGHT_WS_ENDPOINT`)
