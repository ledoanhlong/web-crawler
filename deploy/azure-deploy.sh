#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# Deploy the web-crawler backend to Azure Container Apps.
#
# Prerequisites:
#   1. Azure CLI installed and logged in  (az login)
#   2. Docker installed (for local image build + push)
#
# Usage:
#   chmod +x deploy/azure-deploy.sh
#   ./deploy/azure-deploy.sh
#
# Environment variables (set before running, or in a .env file):
#   AZURE_OPENAI_ENDPOINT       – required
#   AZURE_OPENAI_API_KEY        – required
#   FRONTEND_URL                – your Vercel URL  (e.g. https://my-crawler.vercel.app)
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────
RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-web-crawler-rg}"
LOCATION="${AZURE_LOCATION:-eastus}"
ACR_NAME="${AZURE_ACR_NAME:-webcrawleracr}"
CONTAINER_APP_NAME="${AZURE_CONTAINER_APP_NAME:-web-crawler-api}"
CONTAINER_ENV_NAME="${AZURE_CONTAINER_ENV_NAME:-web-crawler-env}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

# Required env vars
: "${AZURE_OPENAI_ENDPOINT:?Set AZURE_OPENAI_ENDPOINT}"
: "${AZURE_OPENAI_API_KEY:?Set AZURE_OPENAI_API_KEY}"
FRONTEND_URL="${FRONTEND_URL:-}"

echo "══════════════════════════════════════════════════════════════"
echo " Deploying web-crawler backend to Azure Container Apps"
echo "══════════════════════════════════════════════════════════════"

# ── 1. Create resource group ────────────────────────────────────────
echo "→ Creating resource group: $RESOURCE_GROUP"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

# ── 2. Create Azure Container Registry ──────────────────────────────
echo "→ Creating container registry: $ACR_NAME"
az acr create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ACR_NAME" \
  --sku Basic \
  --admin-enabled true \
  --output none

ACR_LOGIN_SERVER=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
ACR_PASSWORD=$(az acr credential show --name "$ACR_NAME" --query "passwords[0].value" -o tsv)

# ── 3. Build & push Docker image ────────────────────────────────────
FULL_IMAGE="${ACR_LOGIN_SERVER}/${CONTAINER_APP_NAME}:${IMAGE_TAG}"
echo "→ Building image: $FULL_IMAGE"
docker build -t "$FULL_IMAGE" .

echo "→ Logging into ACR and pushing image"
docker login "$ACR_LOGIN_SERVER" -u "$ACR_NAME" -p "$ACR_PASSWORD"
docker push "$FULL_IMAGE"

# ── 4. Create Container Apps environment ─────────────────────────────
echo "→ Creating Container Apps environment: $CONTAINER_ENV_NAME"
az containerapp env create \
  --name "$CONTAINER_ENV_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --output none 2>/dev/null || true

# ── 5. Build allowed-origins list ────────────────────────────────────
ALLOWED_ORIGINS="http://localhost:8000,http://localhost:3000"
if [ -n "$FRONTEND_URL" ]; then
  ALLOWED_ORIGINS="${ALLOWED_ORIGINS},${FRONTEND_URL}"
fi

# ── 6. Deploy container app ─────────────────────────────────────────
echo "→ Deploying container app: $CONTAINER_APP_NAME"
az containerapp create \
  --name "$CONTAINER_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --environment "$CONTAINER_ENV_NAME" \
  --image "$FULL_IMAGE" \
  --registry-server "$ACR_LOGIN_SERVER" \
  --registry-username "$ACR_NAME" \
  --registry-password "$ACR_PASSWORD" \
  --target-port 8000 \
  --ingress external \
  --min-replicas 0 \
  --max-replicas 2 \
  --cpu 2 \
  --memory 4Gi \
  --env-vars \
    "AZURE_OPENAI_ENDPOINT=${AZURE_OPENAI_ENDPOINT}" \
    "AZURE_OPENAI_API_KEY=${AZURE_OPENAI_API_KEY}" \
    "AZURE_OPENAI_API_VERSION=${AZURE_OPENAI_API_VERSION:-2025-04-01-preview}" \
    "AZURE_OPENAI_DEPLOYMENT=${AZURE_OPENAI_DEPLOYMENT:-gpt-5.2}" \
    "ALLOWED_ORIGINS=${ALLOWED_ORIGINS}" \
    "FRONTEND_URL=${FRONTEND_URL}" \
    "PLAYWRIGHT_HEADLESS=true" \
    "USE_SCRAPY=false" \
    "LOG_LEVEL=INFO" \
  --output none

# ── 7. Get the app URL ──────────────────────────────────────────────
APP_URL=$(az containerapp show \
  --name "$CONTAINER_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query "properties.configuration.ingress.fqdn" -o tsv)

echo ""
echo "══════════════════════════════════════════════════════════════"
echo " Deployment complete!"
echo ""
echo " Backend URL:  https://${APP_URL}"
echo " Health check: https://${APP_URL}/health"
echo ""
echo " Next steps:"
echo "   1. Set VITE_API_BASE_URL=https://${APP_URL} in your Vercel project"
echo "   2. Re-deploy the Vercel frontend"
echo "══════════════════════════════════════════════════════════════"
