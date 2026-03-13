# Deployment Guide

This project is deployed as two separate services:

- **Frontend** — static HTML served from **Vercel** (free, global CDN)
- **Backend** — Docker container on **Azure Container Apps**, built automatically by GitHub Actions and stored on **GitHub Container Registry** (ghcr.io)

```
  git push
     │
     ▼
┌─────────────────┐    docker image    ┌──────────────┐
│  GitHub Actions  │ ───────────────>  │   ghcr.io    │
│  (builds image)  │                   │  (registry)  │
└─────────────────┘                    └──────┬───────┘
                                              │ pulls image
┌──────────────┐       HTTPS          ┌───────▼────────────────┐
│              │ ────────────────────> │                        │
│  Vercel CDN  │  POST/GET /api/v1/*  │  Azure Container Apps  │
│  (frontend)  │ <─────────────────── │  (backend + Chromium)  │
│              │                      │                        │
└──────────────┘                      └────────┬───────────────┘
   index.html                                  │
   polls job status                            │  Azure OpenAI
   every 1-2s                                  ▼
                                      ┌────────────────────────┐
                                      │  Model Providers        │
                                      │  - OpenAI (gpt-5.2)     │
                                      │  - Vision (gpt-4o)      │
                                      │  - Claude Opus 4.6      │
                                      └────────────────────────┘
```

**How it works:** You push code to GitHub → GitHub Actions automatically builds a Docker image and pushes it to ghcr.io → Azure Container Apps pulls and runs it. No Docker or CLI needed on your machine.

---

## Prerequisites

| Tool | Purpose |
|------|---------|
| GitHub account | Code hosting, container registry (ghcr.io), CI/CD (Actions) |
| Azure subscription | Container Apps hosting |
| Vercel account | Frontend hosting (free tier works) |

No Docker, Azure CLI, or other tools needed on your local machine.

---

## Step 1 — Set up GitHub Actions (automatic image builds)

### 1.1 Push your code to GitHub

```bash
git add -A
git commit -m "prepare for deployment"
git push origin main
```

> **Security:** The `.env` file is in `.gitignore` and `.dockerignore` — your API keys will NOT be uploaded to GitHub or baked into the Docker image.

### 1.2 Verify the build

1. Go to your GitHub repo → **Actions** tab
2. You should see the **"Build & Deploy"** workflow running (triggered by the push to `main`)
3. Wait for it to complete (first build takes ~5-10 min; subsequent builds are faster due to caching)
4. Once green, your image is at `ghcr.io/<your-github-username>/web-crawler:latest`

### 1.3 Make the package public (recommended)

By default ghcr.io packages on private repos are private. To let Azure pull without authentication:

1. Go to your GitHub profile → **Packages** tab
2. Click on the `web-crawler` package
3. **Package settings** → **Danger Zone** → **Change visibility** → **Public**

> If you prefer to keep it private, you'll need to add registry credentials when creating the Container App (see step 2.4 note).

---

## Step 2 — Deploy the Backend via Azure Portal

### 2.1 Create a Resource Group

1. Go to [Azure Portal](https://portal.azure.com)
2. Search for **"Resource groups"** → click **+ Create**
3. **Resource group name:** `web-crawler-rg`
4. **Region:** your preferred region (e.g. `East US`)
5. Click **Review + create** → **Create**

### 2.2 Create a Container Apps Environment

1. Search for **"Container Apps Environments"** → click **+ Create**
2. Set:
   - **Resource group:** `web-crawler-rg`
   - **Name:** `web-crawler-env`
   - **Region:** same region
   - **Plan type:** `Consumption only`
3. Click **Review + create** → **Create**

### 2.3 (Recommended) Create an Azure Files volume for persistent job storage

Without this, job state is lost when the container restarts or scales to zero.

1. Search for **"Storage accounts"** → click **+ Create**
   - **Resource group:** `web-crawler-rg`
   - **Name:** e.g. `webcrawlerstorage` (globally unique)
   - **Region:** same region
   - **Performance:** `Standard`
   - **Redundancy:** `LRS`
   - Click **Review + create** → **Create**
2. Go into your storage account → **Data storage** → **File shares**
   - **+ File share** → Name: `crawler-output` → Tier: `Transaction optimized` → Create
3. Go to your Container Apps Environment (`web-crawler-env`) → **Azure Files** under Settings
   - Click **+ Add**
   - **Name:** `output-volume`
   - **Storage account name:** `webcrawlerstorage`
   - **Storage account key:** (from the storage account → **Access keys**)
   - **File share:** `crawler-output`
   - Click **Add**

### 2.4 Create the Container App

1. Search for **"Container Apps"** → click **+ Create**
2. **Basics** tab:
   - **Resource group:** `web-crawler-rg`
   - **Container app name:** `web-crawler-api`
   - **Container Apps Environment:** `web-crawler-env`
3. **Container** tab:
   - Uncheck **"Use quickstart image"**
   - **Image source:** select **Docker Hub or other registries**
   - **Image and tag:** `ghcr.io/<your-github-username>/web-crawler:latest`
   - **CPU:** `2` cores
   - **Memory:** `4` GB

   > **Private ghcr.io image?** If you didn't make the package public in step 1.3, click **Add registry credentials**: Server = `ghcr.io`, Username = your GitHub username, Password = a GitHub Personal Access Token with `read:packages` scope.

4. **Environment variables** — click **+ Add** for each:

   | Name | Source | Value |
   |------|--------|-------|
   | `AZURE_OPENAI_ENDPOINT` | Manual | `https://your-resource.openai.azure.com/` |
   | `AZURE_OPENAI_API_KEY` | Manual | your key |
   | `AZURE_OPENAI_API_VERSION` | Manual | `2025-04-01-preview` |
   | `AZURE_OPENAI_DEPLOYMENT` | Manual | `gpt-5.2` |
   | `AZURE_VISION_ENDPOINT` | Manual | your vision endpoint |
   | `AZURE_VISION_API_KEY` | Manual | your vision key |
   | `AZURE_VISION_DEPLOYMENT` | Manual | `gpt-4o` |
   | `USE_VISION_PLANNING` | Manual | `true` |
   | `AZURE_CLAUDE_ENDPOINT` | Manual | your Claude endpoint |
   | `AZURE_CLAUDE_API_KEY` | Manual | your Claude key |
   | `AZURE_CLAUDE_DEPLOYMENT` | Manual | `claude-opus-4-6` |
   | `USE_CLAUDE_EXTRACTION` | Manual | `true` |
   | `CLAUDE_FALLBACK_ONLY` | Manual | `true` |
   | `CLAUDE_CIRCUIT_BREAKER_ENABLED` | Manual | `true` |
   | `ALLOWED_ORIGINS` | Manual | `["*"]` (update after Vercel deploy) |
   | `PLAYWRIGHT_HEADLESS` | Manual | `true` |
   | `LOG_LEVEL` | Manual | `INFO` |

5. **Volumes** tab (if you created Azure Files in step 2.3):
   - Click **+ Add volume**
   - **Volume type:** `Azure file volume`
   - **Name:** `output-volume`
   - **File share name:** `crawler-output`
   - Back on the **Container** tab → **Volume mounts** section:
     - **Volume name:** `output-volume`
     - **Mount path:** `/app/output`

6. **Ingress** tab:
   - **Ingress:** `Enabled`
   - **Ingress traffic:** `Accepting traffic from anywhere`
   - **Ingress type:** `HTTP`
   - **Target port:** `8000`

7. **Scaling** tab:
   - **Min replicas:** `0` (scales to zero when idle)
   - **Max replicas:** `2`

8. Click **Review + create** → **Create**

### 2.5 Get your backend URL

1. Go to your Container App → **Overview** page
2. Copy the **Application Url** (e.g. `https://web-crawler-api.nicedesert-abc123.eastus.azurecontainerapps.io`)
3. Open `https://<your-backend-url>/health` in a browser to verify

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
| `VITE_API_BASE_URL` | Your Azure backend URL | `https://web-crawler-api.nicedesert-abc123.eastus.azurecontainerapps.io` |

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

1. Go to Azure Portal → Container Apps → your app → **Containers** → **Environment variables**
2. Update `ALLOWED_ORIGINS` to: `["https://my-crawler.vercel.app","http://localhost:8000"]`
3. Add `FRONTEND_URL` = `https://my-crawler.vercel.app`
4. Click **Save** (triggers a new revision / restart)

> **Important:** `ALLOWED_ORIGINS` must be a JSON array, not comma-separated. E.g. `["https://a.com","https://b.com"]`

---

## Updating the Deployment

### Backend — just push to main

```bash
git push origin main
```

GitHub Actions automatically builds a new image and pushes it to ghcr.io. Then either:

- **Manual update:** Azure Portal → Container App → **Revisions** → **Create new revision** → the image tag `latest` already points to the new build, just save the revision
- **Automatic deploy:** Set up the `AZURE_CREDENTIALS` secret in GitHub to enable the auto-deploy step (see Optional Setup below)

### Frontend — auto-deploys on push

If connected to GitHub, Vercel redeploys automatically when you push to the main branch.

---

## Optional: Enable auto-deploy to Azure from GitHub Actions

The workflow includes a deploy step that's skipped unless you configure Azure credentials. To enable it:

1. In Azure Portal, open **Cloud Shell** (top toolbar icon) and run:
   ```bash
   az ad sp create-for-rbac \
     --name "github-actions-webcrawler" \
     --role contributor \
     --scopes /subscriptions/<your-subscription-id>/resourceGroups/web-crawler-rg \
     --json-auth
   ```
   Copy the entire JSON output.

2. In your GitHub repo → **Settings** → **Secrets and variables** → **Actions**:
   - Add secret `AZURE_CREDENTIALS` = the JSON from step 1
   - Add variable `CONTAINER_APP_NAME` = `web-crawler-api`
   - Add variable `RESOURCE_GROUP` = `web-crawler-rg`

Now every push to `main` will automatically update your Container App with the new image.

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

| Service | Tier | Approximate Cost |
|---------|------|-----------------|
| Azure Container Apps | Consumption (min-replicas=0) | ~$0 idle, ~$0.06/hr when active |
| Azure Files | Pay-as-you-go, LRS | ~$0.01/month (tiny JSON files) |
| Azure OpenAI | Pay-as-you-go | Depends on usage (~$0.01-0.10/crawl) |
| GitHub Container Registry | Free (public) / 500MB free (private) | $0 |
| GitHub Actions | 2,000 min/month free | $0 |
| Vercel | Hobby (free) | $0 |

**Total idle cost: ~$0/month** (no ACR needed). Active cost depends on usage.

---

## Troubleshooting

### GitHub Actions build fails

Go to your repo → **Actions** tab → click the failed run → expand the failed step to see logs.

Common issues:
- Dockerfile syntax error
- Package install failure (check `requirements.txt`)
- ghcr.io permission issue — ensure the workflow has `packages: write` permission

### Backend container won't start

In Azure Portal → Container Apps → your app → **Monitoring** → **Log stream** to see live logs.

Common issues:
- Missing `AZURE_OPENAI_ENDPOINT` or `AZURE_OPENAI_API_KEY` — crashes with `ValidationError`
- `ALLOWED_ORIGINS` set as comma-separated instead of JSON array — crashes with `ValidationError`
- Playwright browser not installed — Dockerfile must have `playwright install chromium`
- Image pull failure — check that ghcr.io package is public or registry credentials are configured

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
4. Test the backend directly: open `https://<backend-url>/health` in a browser

### Crawl jobs disappear after container restart

If you set up Azure Files (step 2.3), jobs persist across restarts. If not, container scale-to-zero loses all job state.

### Container runs out of memory

Playwright + Chromium uses ~300MB. With 4GB allocated, you can run ~3 concurrent browser instances. If you hit OOM:
- Reduce `MAX_CONCURRENT_REQUESTS`
- Increase container memory via Azure Portal
- Use a remote browser service (`PLAYWRIGHT_WS_ENDPOINT`)
