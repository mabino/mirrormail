#!/usr/bin/env zsh
# ==============================================================================
# deploy_azure.sh - Bootstrap Azure Resources & Setup Secretless GitOps Flow
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

# Verify active login
log_info "Verifying Azure subscription..."
if ! az account show &> /dev/null; then
    log_warn "Not logged into Azure. Initiating login flow..."
    az login || { log_error "Failed to log in to Azure."; exit 1; }
fi
SUB_ID=$(az account show --query id -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)
SUB_NAME=$(az account show --query name -o tsv)
log_success "Using Azure subscription: $SUB_NAME"

# Detect GitHub Repository
log_info "Detecting GitHub repository remote..."
GITHUB_REPO=$(git remote get-url origin 2>/dev/null | sed -E 's/.*github.com[:\/]([^.]+)(\.git)?/\1/p')
if [ -z "$GITHUB_REPO" ]; then
    log_warn "Could not detect GitHub repository from git remote."
    read "GITHUB_REPO?Enter your GitHub Repository (e.g. username/repo): "
    if [ -z "$GITHUB_REPO" ]; then
        log_error "GitHub repository is required to setup federated credentials."
        exit 1
    fi
fi
log_success "GitHub Repository detected: $GITHUB_REPO"

# 2. Interactive setup parameters
RAND_SUFFIX=$(LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c 5 2>/dev/null || echo "sync")

echo -e "\n${CYAN}--- Configure Deployment Parameters ---${NC}"
read "RG_NAME?Enter Resource Group name [mirrormail-rg]: "
RG_NAME=${RG_NAME:-mirrormail-rg}

read "LOCATION?Enter Location [eastus]: "
LOCATION=${LOCATION:-eastus}

read "ACR_NAME?Enter Azure Container Registry name [mirrormailacr$RAND_SUFFIX]: "
ACR_NAME=${ACR_NAME:-mirrormailacr$RAND_SUFFIX}
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

# 5. Create Azure Container Registry (ACR)
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

ACR_LOGIN_SERVER=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)

# 6. Create User-Assigned Managed Identity & Federated Credentials for GitHub Actions
log_info "Creating User-Assigned Managed Identity 'mirrormail-github-identity'..."
az identity create --name "mirrormail-github-identity" --resource-group "$RG_NAME" --location "$LOCATION" > /dev/null
IDENTITY_CLIENT_ID=$(az identity show --name "mirrormail-github-identity" --resource-group "$RG_NAME" --query clientId -o tsv)
IDENTITY_PRINCIPAL_ID=$(az identity show --name "mirrormail-github-identity" --resource-group "$RG_NAME" --query principalId -o tsv)

log_info "Assigning RBAC Contributor and AcrPush roles to Managed Identity..."
az role assignment create \
    --role "Contributor" \
    --assignee "$IDENTITY_PRINCIPAL_ID" \
    --scope "/subscriptions/$SUB_ID/resourceGroups/$RG_NAME" > /dev/null 2>&1

az role assignment create \
    --role "AcrPush" \
    --assignee "$IDENTITY_PRINCIPAL_ID" \
    --scope "/subscriptions/$SUB_ID/resourceGroups/$RG_NAME/providers/Microsoft.ContainerRegistry/registries/$ACR_NAME" > /dev/null 2>&1
log_success "Managed Identity role assignments complete."

log_info "Configuring Federated OIDC Credentials for GitHub repo..."
# Clean up existing federated credential to avoid duplication
az identity federated-credential delete \
    --name "mirrormail-github-deploy" \
    --identity-name "mirrormail-github-identity" \
    --resource-group "$RG_NAME" --yes &> /dev/null

az identity federated-credential create \
    --name "mirrormail-github-deploy" \
    --identity-name "mirrormail-github-identity" \
    --resource-group "$RG_NAME" \
    --issuer "https://token.actions.githubusercontent.com" \
    --subject "repo:$GITHUB_REPO:ref:refs/heads/main" \
    --audiences "api://AzureADTokenExchange" > /dev/null
log_success "Federated OIDC Credentials configured."

# 7. Generate GitHub Actions Workflow Files
log_info "Generating GitOps GitHub Actions workflow files..."
mkdir -p .github/workflows

# Deploy Workflow
cat << EOF > .github/workflows/deploy-azure.yml
name: Deploy to Azure (GitOps)
on:
  push:
    branches: [ main ]
permissions:
  id-token: write
  contents: read
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
    - name: Checkout Code
      uses: actions/checkout@v3

    - name: Azure Login
      uses: azure/login@v1
      with:
        client-id: $IDENTITY_CLIENT_ID
        tenant-id: $TENANT_ID
        subscription-id: $SUB_ID

    - name: Build and Push to ACR
      run: |
        az acr login --name $ACR_NAME
        docker build -t $ACR_LOGIN_SERVER/mirrormail:latest .
        docker push $ACR_LOGIN_SERVER/mirrormail:latest

    - name: Deploy Container to ACI
      run: |
        az container delete --resource-group $RG_NAME --name $CONTAINER_NAME --yes || true
        
        STORE_KEY=\$(az storage account keys list --resource-group $RG_NAME --account-name $STORE_NAME --query "[0].value" -o tsv)
        ACR_USERNAME=\$(az acr credential show --name $ACR_NAME --query username -o tsv)
        ACR_PASSWORD=\$(az acr credential show --name $ACR_NAME --query passwords[0].value -o tsv)
        
        az container create \\
            --resource-group $RG_NAME \\
            --name $CONTAINER_NAME \\
            --image $ACR_LOGIN_SERVER/mirrormail:latest \\
            --cpu 1 \\
            --memory 1 \\
            --restart-policy Always \\
            --registry-username "\$ACR_USERNAME" \\
            --registry-password "\$ACR_PASSWORD" \\
            --azure-file-volume-account-name $STORE_NAME \\
            --azure-file-volume-account-key "\$STORE_KEY" \\
            --azure-file-volume-share-name $SHARE_NAME \\
            --azure-file-volume-mount-path "/app/data" \\
            --command-line "python3 bridge_daemon.py --config /app/data/config.json"
EOF

# Teardown Workflow
cat << EOF > .github/workflows/teardown-azure.yml
name: Teardown Azure Resources
on:
  workflow_dispatch:
permissions:
  id-token: write
  contents: read
jobs:
  teardown:
    runs-on: ubuntu-latest
    steps:
    - name: Checkout Code
      uses: actions/checkout@v3

    - name: Azure Login
      uses: azure/login@v1
      with:
        client-id: $IDENTITY_CLIENT_ID
        tenant-id: $TENANT_ID
        subscription-id: $SUB_ID

    - name: Delete Container Instance
      run: |
        az container delete --resource-group $RG_NAME --name $CONTAINER_NAME --yes || true

    - name: Delete Resource Group
      run: |
        az group delete --name $RG_NAME --yes || true
EOF

log_success "GitHub Actions workflow files created successfully!"
echo -e "  -> .github/workflows/deploy-azure.yml"
echo -e "  -> .github/workflows/teardown-azure.yml"

log_info "Bootstrap process complete. To deploy the service:"
echo -e "${YELLOW}1. Commit and push the generated workflow files to GitHub:${NC}"
echo -e "   git add .github/workflows/"
echo -e "   git commit -m 'Configure secretless GitOps Azure deployment workflow'"
echo -e "   git push origin main"
echo -e "${YELLOW}2. Visit your GitHub Actions tab to view the progress of the first deploy!${NC}"
