# Deployment Guide

This project is deployed as two separate services:

- **Frontend** — static HTML served from **Vercel** (free, global CDN)
- **Backend** — Docker container on **Azure Container Apps** (persistent process, Playwright support)

```
┌──────────────┐         HTTPS          ┌────────────────────────┐
│              │  ──────────────────────>│                        │
│  Vercel CDN  │   POST/GET /api/v1/*   │  Azure Container Apps  │
│  (frontend)  │  <──────────────────── │  (backend + Chromium)  │
│              │                        │                        │
└──────────────┘                        └────────┬───────────────┘
   index.html                                    │
   polls job status                               │  Azure OpenAI
   every 1-2s                                    ▼
                                        ┌────────────────────────┐
                                        │  Model Providers        │
                                        │  - OpenAI (gpt-5.2)     │
                                        │  - Vision (gpt-4o)      │
                                        │  - Claude Opus 4.6      │
                                        └────────────────────────┘
```

---

## Prerequisites

| Tool | Purpose |
|------|---------|
| Azure subscription | Container Apps + ACR hosting |
| [Docker Desktop](https://docs.docker.com/get-docker/) | Build container image locally before pushing |
| Vercel account | Frontend hosting (free tier works) |
| GitHub repo | Required for Vercel auto-deploy; optional for Azure |

> **Note:** This guide uses the **Azure Portal** (web UI) for all Azure steps. No Azure CLI is needed. A CLI-based script (`deploy/azure-deploy.sh`) is also available if you prefer.

---

## Step 1 — Deploy the Backend via Azure Portal

### 1.1 Push your code to GitHub

Vercel (Step 2) requires a GitHub repo, and Azure Container Registry can pull images built from GitHub Actions or pushed manually. Make sure your repo is pushed:

```bash
git add -A
git commit -m "prepare for deployment"
git push origin main
```

> **Security:** The `.env` file is in `.gitignore` and `.dockerignore` — your API keys will NOT be uploaded to GitHub or baked into the Docker image.

### 1.2 Create a Resource Group

1. Go to [Azure Portal](https://portal.azure.com)
2. Search for **"Resource groups"** in the top search bar
3. Click **+ Create**
4. Set **Resource group name** to `web-crawler-rg`
5. Set **Region** to your preferred region (e.g. `East US`)
6. Click **Review + create** → **Create**

### 1.3 Create an Azure Container Registry (ACR)

1. Search for **"Container registries"** in the portal
2. Click **+ Create**
3. Set:
   - **Resource group:** `web-crawler-rg`
   - **Registry name:** a globally unique name like `webcrawleracr` (lowercase, no dashes)
   - **Location:** same region as the resource group
   - **SKU:** `Basic`
4. Click **Review + create** → **Create**
5. Once created, go to the registry → **Settings** → **Access keys**
6. Enable **Admin user**
7. Note down: **Login server**, **Username**, and **Password** — you'll need these

### 1.4 Build and push the Docker image

On your local machine, build and push:

```bash
# Log in to your ACR (replace with your values from step 1.3)
docker login <your-acr>.azurecr.io -u <username> -p <password>

# Build the image
docker build -t <your-acr>.azurecr.io/web-crawler-api:latest .

# Push to ACR
docker push <your-acr>.azurecr.io/web-crawler-api:latest
```

### 1.5 Create a Container Apps Environment

1. Search for **"Container Apps Environments"** in the portal
2. Click **+ Create**
3. Set:
   - **Resource group:** `web-crawler-rg`
   - **Name:** `web-crawler-env`
   - **Region:** same region
   - **Plan type:** `Consumption only`
4. Click **Review + create** → **Create**

### 1.6 Create the Container App

1. Search for **"Container Apps"** in the portal
2. Click **+ Create**
3. **Basics** tab:
   - **Resource group:** `web-crawler-rg`
   - **Container app name:** `web-crawler-api`
   - **Container Apps Environment:** `web-crawler-env`
4. **Container** tab:
   - Uncheck **"Use quickstart image"**
   - **Image source:** `Azure Container Registry`
   - **Registry:** your ACR from step 1.3
   - **Image:** `web-crawler-api`
   - **Tag:** `latest`
   - **CPU:** `2` cores
   - **Memory:** `4` GB
5. **Environment variables** — add each of these (click **+ Add**):

   | Name | Value |
   |------|-------|
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

6. **Ingress** tab:
   - **Ingress:** `Enabled`
   - **Ingress traffic:** `Accepting traffic from anywhere`
   - **Ingress type:** `HTTP`
   - **Target port:** `8000`
7. **Scaling** tab:
   - **Min replicas:** `0` (scales to zero when idle)
   - **Max replicas:** `2`
8. Click **Review + create** → **Create**

### 1.7 Get your backend URL

1. Once deployed, go to your Container App in the portal
2. On the **Overview** page, find the **Application Url** (e.g. `https://web-crawler-api.nicedesert-abc123.eastus.azurecontainerapps.io`)
3. Open `https://<your-backend-url>/health` in a browser to verify

---

## Step 2 — Deploy the Frontend on Vercel

### 2.1 Import the project

1. Go to [vercel.com/new](https://vercel.com/new)
2. Import your GitHub repository
3. Set **Root Directory** to `frontend`
4. Vercel auto-detects the build config from `frontend/vercel.json`

### 2.2 Set environment variable

In the Vercel project settings → Environment Variables, add:

| Variable | Value | Example |
|----------|-------|---------|
| `VITE_API_BASE_URL` | Your Azure backend URL | `https://web-crawler-api.nicedesert-abc123.eastus.azurecontainerapps.io` |

### 2.3 Deploy

Click **Deploy** — Vercel runs `node inject-api-url.js`, which:
1. Copies `app/frontend/index.html` to `dist/index.html`
2. Injects a `<meta name="api-base-url">` tag with your backend URL
3. The frontend JavaScript reads this tag to know where to send API requests

### 2.4 Verify

Open your Vercel URL (e.g. `https://my-crawler.vercel.app`) — you should see the Lead Scraper UI. Submit a test crawl to confirm it reaches the backend.

The default UI is sales-facing and hides selector-level controls. Use the `Operator tools` toggle in the header when you need raw method names, diagnostics, selector edits, or JSON troubleshooting.

---

## Step 3 — Connect Frontend ↔ Backend (CORS)

After both are deployed, update the backend's CORS settings so it accepts requests from your Vercel domain.

1. Go to Azure Portal → Container Apps → your app → **Containers** → **Environment variables**
2. Update `ALLOWED_ORIGINS` to: `["https://my-crawler.vercel.app","http://localhost:8000"]`
3. Add `FRONTEND_URL` = `https://my-crawler.vercel.app`
4. Click **Save** (triggers a new revision / restart)

> **Important:** `ALLOWED_ORIGINS` must be a JSON array, not comma-separated. E.g. `["https://a.com","https://b.com"]`

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
| `AZURE_VISION_ENDPOINT` | `""` | Vision endpoint (GPT-4o). Can reuse OpenAI endpoint |
| `AZURE_VISION_API_KEY` | `""` | Vision API key |
| `AZURE_VISION_API_VERSION` | `2025-04-01-preview` | Vision API version |
| `AZURE_VISION_DEPLOYMENT` | `gpt-4o` | Vision deployment name |
| `USE_VISION_PLANNING` | `true` | Enables screenshot-based planning (runs in parallel with HTML analysis, results merged) |
| `AZURE_CLAUDE_ENDPOINT` | `""` | Azure AI Foundry Claude endpoint (root URL or full `/anthropic/v1/messages`) |
| `AZURE_CLAUDE_API_KEY` | `""` | Claude API key |
| `AZURE_CLAUDE_DEPLOYMENT` | `claude-opus-4-6` | Claude model deployment name |
| `USE_CLAUDE_EXTRACTION` | `true` | Enables Claude extraction fallback |
| `USE_SCRIPT_EXTRACTION` | `true` | Enables generated-script extraction in preview/full crawl |
| `ALLOW_GENERATED_SCRIPT_EXECUTION` | `false` | Standalone script endpoints only auto-run generated code when this is true |
| `CLAUDE_CIRCUIT_BREAKER_ENABLED` | `true` | Temporarily disables Claude after repeated failures |
| `CLAUDE_CIRCUIT_BREAKER_MAX_ERRORS` | `3` | Consecutive Claude errors before breaker opens |
| `CLAUDE_CIRCUIT_BREAKER_COOLDOWN_S` | `600` | Breaker cooldown window |
| `CLAUDE_FALLBACK_ONLY` | `true` | OpenAI-first policy; Claude only on fallback |
| `CLAUDE_MAX_RETRIES_PER_STAGE` | `1` | Max Claude attempts per scrape stage |
| `OPENAI_INPUT_COST_PER_MTOK` | `2.5` | Cost estimate input tokens per 1M (USD) |
| `OPENAI_OUTPUT_COST_PER_MTOK` | `10.0` | Cost estimate output tokens per 1M (USD) |
| `CLAUDE_INPUT_COST_PER_MTOK` | `15.0` | Cost estimate input tokens per 1M (USD) |
| `CLAUDE_OUTPUT_COST_PER_MTOK` | `75.0` | Cost estimate output tokens per 1M (USD) |
| `ALLOWED_ORIGINS` | `["*"]` | CORS allowed origins (**JSON array**, not comma-separated) |
| `FRONTEND_URL` | `""` | Vercel frontend URL |
| `OUTPUT_DIR` | `./output` | Results storage directory |
| `MAX_JOB_DURATION_S` | `7200` | Max crawl duration (seconds) |
| `PLAYWRIGHT_HEADLESS` | `true` | Run browser headless |
| `PLAYWRIGHT_WS_ENDPOINT` | `null` | Remote browser endpoint (Browserless, etc.) |
| `USE_SMART_SCRAPER_PRIMARY` | `true` | Use ScrapeGraphAI as primary extractor |
| `USE_SCRAPY` | `false` | Enable Scrapy subprocess |
| `USE_CRAWL4AI` | `false` | Enable Crawl4AI markdown extractor |
| `USE_UNIVERSAL_SCRAPER` | `false` | Enable AI-powered BS4 code generation |
| `USE_LISTING_API_INTERCEPTION` | `true` | Allow preview/full crawl to intercept listing APIs when structured data is loaded in the background |
| `USE_INNER_TEXT_FALLBACK` | `true` | Allow markdown/HTML extraction to fall back to rendered `innerText` on shadow-DOM or SPA shells |
| `MAX_CONCURRENT_REQUESTS` | `5` | HTTP concurrency limit |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

### Frontend (Vercel)

| Variable | Description |
|----------|-------------|
| `VITE_API_BASE_URL` | Backend URL (injected at build time) |

---

## Updating the Deployment

### Backend — push a new image

```bash
# Rebuild and push
docker build -t <your-acr>.azurecr.io/web-crawler-api:v1.1 .
docker push <your-acr>.azurecr.io/web-crawler-api:v1.1
```

Then in Azure Portal → Container Apps → your app → **Revisions** → **Create new revision** → update the image tag to `v1.1`.

### Frontend — auto-deploys on push

If connected to GitHub, Vercel redeploys automatically when you push to the main branch. The build script re-reads `app/frontend/index.html` from the repo root each time.

---

## Local Development

No deployment config changes are needed for local dev. The defaults are backward-compatible:

```bash
# Backend
python -m uvicorn app.main:app --reload
# Runs at http://localhost:8000, serves frontend + API

# Frontend (standalone, optional)
cd frontend
VITE_API_BASE_URL=http://localhost:8000 node inject-api-url.js
# Open dist/index.html in a browser
```

When no `VITE_API_BASE_URL` is set, the frontend falls back to `window.location.origin` — which is `http://localhost:8000` when served by the backend directly.

---

## Cost Estimate

| Service | Tier | Approximate Cost |
|---------|------|-----------------|
| Azure Container Apps | Consumption (min-replicas=0) | ~$0 idle, ~$0.06/hr when active |
| Azure Container Registry | Basic | ~$5/month |
| Azure OpenAI | Pay-as-you-go | Depends on usage (~$0.01-0.10/crawl) |
| Vercel | Hobby (free) | $0 |

**Total idle cost: ~$5/month** (just the container registry).

---

## Troubleshooting

### Backend container won't start

In Azure Portal → Container Apps → your app → **Monitoring** → **Log stream** to see live container logs.

Common issues:
- Missing `AZURE_OPENAI_ENDPOINT` or `AZURE_OPENAI_API_KEY` — container crashes on startup with a `ValidationError`
- Playwright browser not installed — ensure `playwright install chromium` runs in Dockerfile
- Claude endpoint mismatch — use Azure AI Foundry endpoint (root URL or full `/anthropic/v1/messages`)
- `ALLOWED_ORIGINS` set as comma-separated instead of JSON array — causes `ValidationError`

### `/health` returns `degraded`

`/health` now includes provider readiness. `degraded` means at least one provider failed check.

Common causes:
- Vision provider not configured but `USE_VISION_PLANNING=true` (vision runs in parallel with HTML planning — both are needed)
- Claude auth/endpoint issue
- Claude circuit breaker open after repeated runtime failures

Use:
- `GET /health` to inspect provider statuses
- `GET /api/v1/crawl/<job-id>/telemetry` to inspect provider events, fallback reasons, latency, and estimated costs

### Frontend shows errors / can't reach backend

1. Open browser DevTools → Console — look for CORS errors
2. Verify `VITE_API_BASE_URL` was set in Vercel before deploying
3. Check that `ALLOWED_ORIGINS` on the backend includes your Vercel URL (must be JSON array)
4. Test the backend directly: open `https://<backend-url>/health` in a browser
5. If jobs complete but quality is low, inspect provider telemetry: `GET /api/v1/crawl/<job-id>/telemetry`

### Crawl jobs disappear after container restart

Jobs are snapshotted to `OUTPUT_DIR/job_store`, so a process restart on the same persistent filesystem can recover jobs. Container scale-to-zero without a mounted volume still loses job state. For production durability, mount an Azure Files volume or use a database-backed job store.

### Container runs out of memory

Playwright + Chromium uses ~300MB. With 4GB allocated, you can run ~3 concurrent browser instances. If you hit OOM:
- Reduce `MAX_CONCURRENT_REQUESTS`
- Increase container memory via Azure Portal → Container App → Scale
- Use a remote browser service (`PLAYWRIGHT_WS_ENDPOINT`)
