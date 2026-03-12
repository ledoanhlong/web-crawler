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

| Tool | Version | Purpose |
|------|---------|---------|
| [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) | >= 2.60 | Create Azure resources |
| [Docker](https://docs.docker.com/get-docker/) | >= 24 | Build & push container image |
| [Node.js](https://nodejs.org/) | >= 18 | Vercel frontend build script |
| [Vercel CLI](https://vercel.com/docs/cli) (optional) | latest | Deploy frontend from terminal |
| Azure subscription | — | Container Apps + ACR hosting |
| Vercel account | — | Frontend hosting (free tier works) |

---

## Step 1 — Deploy the Backend on Azure

### 1.1 Set environment variables

```bash
# Required — Azure OpenAI credentials
export AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com/"
export AZURE_OPENAI_API_KEY="your-api-key-here"

# Optional — override defaults
export AZURE_OPENAI_API_VERSION="2025-04-01-preview"
export AZURE_OPENAI_DEPLOYMENT="gpt-5.2"

# Optional — Vision provider (GPT-4o)
export AZURE_VISION_ENDPOINT="https://your-resource.openai.azure.com/"
export AZURE_VISION_API_KEY="your-api-key-here"
export AZURE_VISION_DEPLOYMENT="gpt-4o"
export USE_VISION_PLANNING="true"

# Optional — Claude provider (Azure AI Foundry Anthropic endpoint)
export AZURE_CLAUDE_ENDPOINT="https://your-claude-deployment.services.ai.azure.com/"
export AZURE_CLAUDE_API_KEY="your-claude-api-key-here"
export AZURE_CLAUDE_DEPLOYMENT="claude-opus-4-6"
export USE_CLAUDE_EXTRACTION="true"

# Optional — Claude circuit breaker + cost-control policy
export CLAUDE_CIRCUIT_BREAKER_ENABLED="true"
export CLAUDE_CIRCUIT_BREAKER_MAX_ERRORS="3"
export CLAUDE_CIRCUIT_BREAKER_COOLDOWN_S="600"
export CLAUDE_FALLBACK_ONLY="true"
export CLAUDE_MAX_RETRIES_PER_STAGE="1"

# Optional — Azure resource naming
export AZURE_RESOURCE_GROUP="web-crawler-rg"
export AZURE_LOCATION="eastus"
export AZURE_ACR_NAME="webcrawleracr"
export AZURE_CONTAINER_APP_NAME="web-crawler-api"
```

### 1.2 Log in to Azure

```bash
az login
```

### 1.3 Run the deployment script

```bash
chmod +x deploy/azure-deploy.sh
./deploy/azure-deploy.sh
```

This script will:
1. Create a resource group
2. Create an Azure Container Registry (ACR)
3. Build the Docker image and push it to ACR
4. Create a Container Apps environment
5. Deploy the container with all environment variables
6. Print the backend URL (e.g. `https://web-crawler-api.nicedesert-abc123.eastus.azurecontainerapps.io`)

### 1.4 Verify

```bash
curl https://<your-backend-url>/health
# → {"status":"ok|degraded","providers":{...}}

# Optional: provider-level per-job telemetry after a crawl
curl https://<your-backend-url>/api/v1/crawl/<job-id>/telemetry
```

### Container Resources

The deployment script configures:

| Setting | Value | Notes |
|---------|-------|-------|
| CPU | 2 cores | Playwright + LLM calls need headroom |
| Memory | 4 GB | Chromium uses ~300MB per instance |
| Min replicas | 0 | Scales to zero when idle (saves cost) |
| Max replicas | 2 | Handles concurrent crawl jobs |
| Ingress | External | Public HTTPS endpoint |
| Port | 8000 | Uvicorn default |

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

---

## Step 3 — Connect Frontend ↔ Backend (CORS)

After both are deployed, update the backend's CORS settings so it accepts requests from your Vercel domain.

### Option A: Re-run the deploy script with `FRONTEND_URL`

```bash
export FRONTEND_URL="https://my-crawler.vercel.app"
./deploy/azure-deploy.sh
```

### Option B: Update the env var directly in Azure Portal

1. Go to Azure Portal → Container Apps → your app → Settings → Environment variables
2. Set `ALLOWED_ORIGINS` to: `["https://my-crawler.vercel.app","http://localhost:8000"]`
3. Set `FRONTEND_URL` to: `https://my-crawler.vercel.app`
4. Click **Save** (triggers a restart)

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
| `CLAUDE_CIRCUIT_BREAKER_ENABLED` | `true` | Temporarily disables Claude after repeated failures |
| `CLAUDE_CIRCUIT_BREAKER_MAX_ERRORS` | `3` | Consecutive Claude errors before breaker opens |
| `CLAUDE_CIRCUIT_BREAKER_COOLDOWN_S` | `600` | Breaker cooldown window |
| `CLAUDE_FALLBACK_ONLY` | `true` | OpenAI-first policy; Claude only on fallback |
| `CLAUDE_MAX_RETRIES_PER_STAGE` | `1` | Max Claude attempts per scrape stage |
| `OPENAI_INPUT_COST_PER_MTOK` | `2.5` | Cost estimate input tokens per 1M (USD) |
| `OPENAI_OUTPUT_COST_PER_MTOK` | `10.0` | Cost estimate output tokens per 1M (USD) |
| `CLAUDE_INPUT_COST_PER_MTOK` | `15.0` | Cost estimate input tokens per 1M (USD) |
| `CLAUDE_OUTPUT_COST_PER_MTOK` | `75.0` | Cost estimate output tokens per 1M (USD) |
| `ALLOWED_ORIGINS` | `["*"]` | CORS allowed origins (JSON list) |
| `FRONTEND_URL` | `""` | Vercel frontend URL |
| `OUTPUT_DIR` | `./output` | Results storage directory |
| `MAX_JOB_DURATION_S` | `7200` | Max crawl duration (seconds) |
| `PLAYWRIGHT_HEADLESS` | `true` | Run browser headless |
| `PLAYWRIGHT_WS_ENDPOINT` | `null` | Remote browser endpoint (Browserless, etc.) |
| `USE_SMART_SCRAPER_PRIMARY` | `true` | Use ScrapeGraphAI as primary extractor |
| `USE_SCRAPY` | `false` | Enable Scrapy subprocess |
| `USE_CRAWL4AI` | `false` | Enable Crawl4AI markdown extractor |
| `USE_UNIVERSAL_SCRAPER` | `false` | Enable AI-powered BS4 code generation |
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
IMAGE_TAG="v1.1" ./deploy/azure-deploy.sh
```

Or update the container image directly:
```bash
az containerapp update \
  --name web-crawler-api \
  --resource-group web-crawler-rg \
  --image webcrawleracr.azurecr.io/web-crawler-api:v1.1
```

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

Check container logs:
```bash
az containerapp logs show \
  --name web-crawler-api \
  --resource-group web-crawler-rg \
  --follow
```

Common issues:
- Missing `AZURE_OPENAI_ENDPOINT` or `AZURE_OPENAI_API_KEY` — container crashes on startup
- Playwright browser not installed — ensure `playwright install chromium` runs in Dockerfile
- Claude endpoint mismatch — use Azure AI Foundry endpoint (root URL or full `/anthropic/v1/messages`)

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
3. Check that `ALLOWED_ORIGINS` on the backend includes your Vercel URL
4. Test the backend directly: `curl https://<backend-url>/health`
5. If jobs complete but quality is low, inspect provider telemetry: `curl https://<backend-url>/api/v1/crawl/<job-id>/telemetry`

### Crawl jobs disappear after container restart

This is expected — job state is held in memory. The container scales to zero after idle timeout, losing all active jobs. For production use with persistent jobs, consider adding a database for job state (future enhancement).

### Container runs out of memory

Playwright + Chromium uses ~300MB. With 4GB allocated, you can run ~3 concurrent browser instances. If you hit OOM:
- Reduce `MAX_CONCURRENT_REQUESTS`
- Increase container memory via Azure Portal
- Use a remote browser service (`PLAYWRIGHT_WS_ENDPOINT`)
