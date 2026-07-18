#!/usr/bin/env zsh
# ==============================================================================
# teardown_gcp.sh - Clean up all deployed Mirrormail resources on GCP
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
if ! command -v gcloud &> /dev/null; then
    log_error "Google Cloud SDK ('gcloud') is not installed."
    exit 1
fi

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
    log_error "No active GCP project set. Please set it using 'gcloud config set project'."
    exit 1
fi
log_info "Using GCP Project: $PROJECT_ID"

# 2. Parameters Configuration
echo -e "\n${CYAN}--- Configure Teardown Parameters ---${NC}"
read "REGION?Enter GCP Region [us-central1]: "
REGION=${REGION:-us-central1}

read "JOB_NAME?Enter Cloud Run Job name [mirrormail-sync]: "
JOB_NAME=${JOB_NAME:-mirrormail-sync}

read "BUCKET_NAME?Enter Storage Bucket name [mirrormail-data-$PROJECT_ID]: "
BUCKET_NAME=${BUCKET_NAME:-mirrormail-data-$PROJECT_ID}

read "REPO_NAME?Enter Artifact Registry Repository name [mirrormail-repo]: "
REPO_NAME=${REPO_NAME:-mirrormail-repo}

# Prompt for confirmation
log_warn "This action will permanently delete:"
echo -e " - Cloud Run Job: $JOB_NAME"
echo -e " - Cloud Scheduler Trigger: $JOB_NAME-trigger"
echo -e " - Storage Bucket and all synced SQLite history/config: gs://$BUCKET_NAME"
echo -e " - Artifact Registry Docker Repository: $REPO_NAME"
read "CONFIRM?Are you sure you want to proceed with teardown? (y/N): "
CONFIRM=${CONFIRM:-n}

if [[ ! "$CONFIRM" =~ ^[yY]$ ]]; then
    log_info "Teardown cancelled."
    exit 0
fi

# 3. Teardown logic
SCHEDULER_JOB_NAME="$JOB_NAME-trigger"
log_info "Deleting Cloud Scheduler trigger '$SCHEDULER_JOB_NAME'..."
if gcloud scheduler jobs describe "$SCHEDULER_JOB_NAME" --location="$REGION" &> /dev/null; then
    gcloud scheduler jobs delete "$SCHEDULER_JOB_NAME" --location="$REGION" --quiet
    log_success "Scheduler trigger deleted."
else
    log_info "Scheduler trigger not found."
fi

log_info "Deleting Cloud Run Job '$JOB_NAME'..."
if gcloud run jobs describe "$JOB_NAME" --region="$REGION" &> /dev/null; then
    gcloud run jobs delete "$JOB_NAME" --region="$REGION" --quiet
    log_success "Cloud Run Job deleted."
else
    log_info "Cloud Run Job not found."
fi

log_info "Deleting GCS Bucket 'gs://$BUCKET_NAME'..."
if gcloud storage buckets describe "gs://$BUCKET_NAME" &> /dev/null; then
    # --recursive ensures all objects (SQLite db, config.json) are also deleted
    gcloud storage buckets delete "gs://$BUCKET_NAME" --quiet
    log_success "GCS Bucket deleted."
else
    log_info "GCS Bucket not found."
fi

log_info "Deleting Artifact Registry Repository '$REPO_NAME'..."
if gcloud artifacts repositories describe "$REPO_NAME" --location="$REGION" &> /dev/null; then
    gcloud artifacts repositories delete "$REPO_NAME" --location="$REGION" --quiet
    log_success "Artifact Registry Repository deleted."
else
    log_info "Artifact Registry Repository not found."
fi

log_success "GCP teardown complete!"
