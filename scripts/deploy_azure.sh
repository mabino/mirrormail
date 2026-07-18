#!/usr/bin/env zsh
# ==============================================================================
# deploy_azure.sh - Deploy Mirrormail to Azure Container Instances (ACI)
# ==============================================================================

# Color escape codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# 1. Dependency checks
log_info "Checking Azure CLI installation..."
if ! command -v az &> /dev/null; then
    log_error "Azure CLI ('az') is not installed."
    log_warn "Install it via Homebrew: brew install azure-cli"
    exit 1
fi
log_success "Azure CLI is installed."

log_info "Checking Docker installation..."
if ! command -v docker &> /dev/null; then
    log_error "Docker is not installed or running."
    exit 1
fi
log_success "Docker is running."

# Verify active login
log_info "Verifying Azure subscription..."
if ! az account show &> /dev/null; then
    log_warn "Not logged into Azure. Initiating login flow..."
    az login || { log_error "Failed to log in to Azure."; exit 1; }
fi
SUB_NAME=$(az account show --query name -o tsv)
log_success "Using Azure subscription: $SUB_NAME"

# 2. Interactive setup parameters
RAND_SUFFIX=$(LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c 5 2>/dev/null || echo "sync")

echo -e "\n${CYAN}--- Configure Deployment Parameters ---${NC}"
read "RG_NAME?Enter Resource Group name [mirrormail-rg]: "
RG_NAME=${RG_NAME:-mirrormail-rg}

read "LOCATION?Enter Location [eastus]: "
LOCATION=${LOCATION:-eastus}

read "ACR_NAME?Enter Azure Container Registry name [mirrormailacr$RAND_SUFFIX]: "
ACR_NAME=${ACR_NAME:-mirrormailacr$RAND_SUFFIX}
# Lowercase only for ACR name
ACR_NAME=$(echo "$ACR_NAME" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9')

read "STORE_NAME?Enter Storage Account name (for SQLite/config) [mirrormailstore$RAND_SUFFIX]: "
STORE_NAME=${STORE_NAME:-mirrormailstore$RAND_SUFFIX}
STORE_NAME=$(echo "$STORE_NAME" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9')

read "SHARE_NAME?Enter Storage File Share name [mirrormail-data]: "
SHARE_NAME=${SHARE_NAME:-mirrormail-data}

read "CONTAINER_NAME?Enter Container Group name [mirrormail-bridge]: "
CONTAINER_NAME=${CONTAINER_NAME:-mirrormail-bridge}

# Verify local config.json exists
if [ ! -f "config.json" ]; then
    log_error "config.json not found in the current directory."
    log_warn "Please run 'python3 auth_setup.py' first to generate your configuration."
    exit 1
fi

# 3. Create Resource Group (Idempotent)
log_info "Verifying Resource Group '$RG_NAME'..."
if ! az group exists -n "$RG_NAME" -o tsv | grep -q "true"; then
    log_info "Creating Resource Group '$RG_NAME' in '$LOCATION'..."
    az group create -n "$RG_NAME" -l "$LOCATION" > /dev/null
    log_success "Resource Group created."
else
    log_success "Resource Group already exists."
fi

# 4. Create Storage Account & File Share (Idempotent)
log_info "Verifying Storage Account '$STORE_NAME'..."
STORE_EXISTS=$(az storage account check-name --name "$STORE_NAME" --query nameAvailable -o tsv)
if [ "$STORE_EXISTS" = "true" ]; then
    log_info "Creating Storage Account '$STORE_NAME'..."
    az storage account create \
        --resource-group "$RG_NAME" \
        --name "$STORE_NAME" \
        --location "$LOCATION" \
        --sku Standard_LRS \
        --allow-blob-public-access false > /dev/null
    log_success "Storage Account created."
else
    log_success "Storage Account already exists."
fi

# Get storage key
STORE_KEY=$(az storage account keys list --resource-group "$RG_NAME" --account-name "$STORE_NAME" --query "[0].value" -o tsv)

log_info "Verifying File Share '$SHARE_NAME'..."
SHARE_EXISTS=$(az storage share exists --account-name "$STORE_NAME" --account-key "$STORE_KEY" --name "$SHARE_NAME" --query exists -o tsv)
if [ "$SHARE_EXISTS" = "false" ]; then
    log_info "Creating File Share '$SHARE_NAME'..."
    az storage share create --account-name "$STORE_NAME" --account-key "$STORE_KEY" --name "$SHARE_NAME" > /dev/null
    log_success "File Share created."
else
    log_success "File Share already exists."
fi

# Upload config.json
log_info "Uploading local config.json to the File Share..."
# Modify database path to point inside the volume mount for container persistence
tmp_config=$(mktemp)
cat config.json | sed 's/"database_path": ".*"/"database_path": "\/app\/data\/email_bridge.db"/g' > "$tmp_config"

az storage file upload \
    --account-name "$STORE_NAME" \
    --account-key "$STORE_KEY" \
    --share-name "$SHARE_NAME" \
    --source "$tmp_config" \
    --path "config.json" > /dev/null
rm "$tmp_config"
log_success "config.json uploaded successfully."

# 5. Create Azure Container Registry (Idempotent)
log_info "Verifying Container Registry '$ACR_NAME'..."
ACR_EXISTS=$(az acr check-name --name "$ACR_NAME" --query nameAvailable -o tsv)
if [ "$ACR_EXISTS" = "true" ]; then
    log_info "Creating Container Registry '$ACR_NAME'..."
    az acr create \
        --resource-group "$RG_NAME" \
        --name "$ACR_NAME" \
        --sku Basic \
        --admin-enabled true > /dev/null
    log_success "ACR created."
else
    log_success "ACR already exists."
fi

# Get ACR credentials
ACR_LOGIN_SERVER=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
ACR_USERNAME=$(az acr credential show --name "$ACR_NAME" --query username -o tsv)
ACR_PASSWORD=$(az acr credential show --name "$ACR_NAME" --query passwords[0].value -o tsv)

# 6. Build and Push Docker image
IMAGE_TAG="$ACR_LOGIN_SERVER/mirrormail:latest"
log_info "Building Docker image: $IMAGE_TAG..."
docker build -t "$IMAGE_TAG" . || { log_error "Docker build failed."; exit 1; }
log_success "Docker image built."

log_info "Logging into ACR..."
az acr login --name "$ACR_NAME" > /dev/null

log_info "Pushing image to ACR..."
docker push "$IMAGE_TAG" || { log_error "Docker push failed."; exit 1; }
log_success "Image pushed to registry."

# 7. Deploy to ACI with mounts
log_info "Deploying to Azure Container Instances..."
# Check if container group already exists, delete if so to make update idempotent
az container delete --resource-group "$RG_NAME" --name "$CONTAINER_NAME" --yes &> /dev/null

az container create \
    --resource-group "$RG_NAME" \
    --name "$CONTAINER_NAME" \
    --image "$IMAGE_TAG" \
    --cpu 1 \
    --memory 1 \
    --restart-policy Always \
    --registry-username "$ACR_USERNAME" \
    --registry-password "$ACR_PASSWORD" \
    --azure-file-volume-account-name "$STORE_NAME" \
    --azure-file-volume-account-key "$STORE_KEY" \
    --azure-file-volume-share-name "$SHARE_NAME" \
    --azure-file-volume-mount-path "/app/data" \
    --command-line "python3 bridge_daemon.py --config /app/data/config.json" > /dev/null

log_success "Mirrormail deployed to Azure Container Instances successfully!"
log_info "You can view execution logs by running:"
echo -e "${CYAN}  az container logs --resource-group \"$RG_NAME\" --name \"$CONTAINER_NAME\"${NC}"
