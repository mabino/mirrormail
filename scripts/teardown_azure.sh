#!/usr/bin/env zsh
# ==============================================================================
# teardown_azure.sh - Clean up all deployed Mirrormail resources on Azure
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
if ! command -v az &> /dev/null; then
    log_error "Azure CLI ('az') is not installed."
    exit 1
fi

# Verify active login
if ! az account show &> /dev/null; then
    log_warn "Not logged into Azure. Initiating login flow..."
    az login || { log_error "Failed to log in to Azure."; exit 1; }
fi

# 2. Parameters Configuration
echo -e "\n${CYAN}--- Configure Teardown Parameters ---${NC}"
read "RG_NAME?Enter Resource Group name [mirrormail-rg]: "
RG_NAME=${RG_NAME:-mirrormail-rg}

read "CONTAINER_NAME?Enter Container Group name to delete [mirrormail-bridge]: "
CONTAINER_NAME=${CONTAINER_NAME:-mirrormail-bridge}

# Prompt for confirmation
log_warn "This action will permanently delete:"
echo -e " - Container Group: $CONTAINER_NAME"
echo -e " - Entire Resource Group (including ACR registries & Storage Accounts) if you choose to delete the Resource Group."
read "CONFIRM?Are you sure you want to proceed with teardown? (y/N): "
CONFIRM=${CONFIRM:-n}

if [[ ! "$CONFIRM" =~ ^[yY]$ ]]; then
    log_info "Teardown cancelled."
    exit 0
fi

# 3. Teardown logic
log_info "Verifying Container Group '$CONTAINER_NAME'..."
if az container show --resource-group "$RG_NAME" --name "$CONTAINER_NAME" &> /dev/null; then
    log_info "Deleting Container Instance '$CONTAINER_NAME'..."
    az container delete --resource-group "$RG_NAME" --name "$CONTAINER_NAME" --yes
    log_success "Container Group deleted."
else
    log_info "Container Group not found."
fi

# Ask if we should delete the entire Resource Group
read "DELETE_RG?Do you want to delete the ENTIRE Resource Group '$RG_NAME'? (y/N): "
DELETE_RG=${DELETE_RG:-n}

if [[ "$DELETE_RG" =~ ^[yY]$ ]]; then
    log_info "Deleting entire Resource Group '$RG_NAME' (this may take a few minutes)..."
    az group delete --name "$RG_NAME" --yes
    log_success "Resource Group '$RG_NAME' and all nested resources deleted successfully."
else
    log_info "Skipped deleting Resource Group '$RG_NAME'."
fi

log_success "Azure teardown complete!"
