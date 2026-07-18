#!/usr/bin/env zsh
# ==============================================================================
# deploy_gcp.sh - Deploy Mirrormail to GCP Cloud Run Jobs + Cloud Scheduler
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
log_info "Checking Google Cloud SDK installation..."
if ! command -v gcloud &> /dev/null; then
    log_error "Google Cloud SDK ('gcloud') is not installed."
    log_warn "Install it via Homebrew: brew install --cask google-cloud-sdk"
    exit 1
fi
log_success "Google Cloud SDK is installed."

log_info "Checking Docker installation..."
if ! command -v docker &> /dev/null; then
    log_error "Docker is not installed or running."
    exit 1
fi
log_success "Docker is running."

# Get active project
PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
    log_warn "No active GCP project set."
    read "PROJECT_ID?Enter your Google Cloud Project ID: "
    if [ -z "$PROJECT_ID" ]; then
        log_error "GCP Project ID is required."
        exit 1
    fi
    gcloud config set project "$PROJECT_ID"
fi
log_success "Using GCP Project ID: $PROJECT_ID"

# 2. Interactive setup parameters
echo -e "\n${CYAN}--- Configure Deployment Parameters ---${NC}"
read "REGION?Enter GCP Region [us-central1]: "
REGION=${REGION:-us-central1}

read "REPO_NAME?Enter Artifact Registry Repository name [mirrormail-repo]: "
REPO_NAME=${REPO_NAME:-mirrormail-repo}

read "BUCKET_NAME?Enter Cloud Storage Bucket name for persistent database [mirrormail-data-$PROJECT_ID]: "
BUCKET_NAME=${BUCKET_NAME:-mirrormail-data-$PROJECT_ID}
BUCKET_NAME=$(echo "$BUCKET_NAME" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-')

read "JOB_NAME?Enter Cloud Run Job name [mirrormail-sync]: "
JOB_NAME=${JOB_NAME:-mirrormail-sync}

# Verify local config.json exists
if [ ! -f "config.json" ]; then
    log_error "config.json not found in the current directory."
    log_warn "Please run 'python3 auth_setup.py' first to generate your configuration."
    exit 1
fi

# 3. Enable APIs (Idempotent)
log_info "Enabling required Google APIs (Artifact Registry, Cloud Run, Cloud Scheduler)..."
gcloud services enable \
    artifactregistry.googleapis.com \
    run.googleapis.com \
    cloudscheduler.googleapis.com > /dev/null
log_success "Required APIs enabled."

# 4. Create Artifact Registry (Idempotent)
log_info "Verifying Artifact Registry repository '$REPO_NAME' in '$REGION'..."
if ! gcloud artifacts repositories describe "$REPO_NAME" --location="$REGION" &> /dev/null; then
    log_info "Creating Artifact Registry repository '$REPO_NAME'..."
    gcloud artifacts repositories create "$REPO_NAME" \
        --repository-format=docker \
        --location="$REGION" \
        --description="Docker repository for Mirrormail bridge" > /dev/null
    log_success "Repository created."
else
    log_success "Repository already exists."
fi

# 5. Build and Push Docker image
IMAGE_TAG="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/mirrormail:latest"
log_info "Building Docker image: $IMAGE_TAG..."
docker build -t "$IMAGE_TAG" . || { log_error "Docker build failed."; exit 1; }
log_success "Docker image built."

log_info "Configuring Docker authentication for GCP..."
gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet > /dev/null

log_info "Pushing image to Artifact Registry..."
docker push "$IMAGE_TAG" || { log_error "Docker push failed."; exit 1; }
log_success "Image pushed to Artifact Registry."

# 6. Create GCS Bucket for persistence
log_info "Verifying Storage Bucket 'gs://$BUCKET_NAME'..."
if ! gcloud storage buckets describe "gs://$BUCKET_NAME" &> /dev/null; then
    log_info "Creating Storage Bucket 'gs://$BUCKET_NAME'..."
    gcloud storage buckets create "gs://$BUCKET_NAME" --location="$REGION" > /dev/null
    log_success "Storage Bucket created."
else
    log_success "Storage Bucket already exists."
fi

# Upload config.json to Bucket
log_info "Uploading local config.json to Storage Bucket..."
tmp_config=$(mktemp)
cat config.json | sed 's/"database_path": ".*"/"database_path": "\/app\/data\/email_bridge.db"/g' > "$tmp_config"
gcloud storage cp "$tmp_config" "gs://$BUCKET_NAME/config.json" > /dev/null
rm "$tmp_config"
log_success "config.json uploaded to GCS."

# 7. Deploy Cloud Run Job with Cloud Storage FUSE volume mount
log_info "Deploying Cloud Run Job '$JOB_NAME'..."
# Clean up existing job to ensure update is idempotent and applies new volume config
gcloud run jobs delete "$JOB_NAME" --region="$REGION" --quiet &> /dev/null

gcloud run jobs deploy "$JOB_NAME" \
    --image "$IMAGE_TAG" \
    --region "$REGION" \
    --command "python3" \
    --args "bridge_daemon.py,--config,/app/data/config.json,--one-shot" \
    --add-volume="name=data-vol,type=cloud-storage,bucket=$BUCKET_NAME" \
    --add-volume-mount="volume=data-vol,mount-path=/app/data" > /dev/null
log_success "Cloud Run Job deployed."

# 8. Setup Cloud Scheduler trigger (runs every 5 minutes)
SCHEDULER_JOB_NAME="$JOB_NAME-trigger"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --query projectNumber --format "value(projectNumber)")

log_info "Verifying Cloud Scheduler trigger '$SCHEDULER_JOB_NAME'..."
# Check if trigger already exists, delete if so to update it idempotently
gcloud scheduler jobs delete "$SCHEDULER_JOB_NAME" --location="$REGION" --quiet &> /dev/null

log_info "Creating Cloud Scheduler trigger to run every 5 minutes..."
gcloud scheduler jobs create http "$SCHEDULER_JOB_NAME" \
    --schedule="*/5 * * * *" \
    --location="$REGION" \
    --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/$JOB_NAME:run" \
    --http-method=POST \
    --oauth-service-account-email="$PROJECT_NUMBER-compute@developer.gserviceaccount.com" > /dev/null

log_success "Cloud Scheduler trigger configured."
log_success "Mirrormail deployed to GCP successfully!"
log_info "Your daemon will execute on Cloud Run every 5 minutes, backed by persistent SQLite storage in GCS."
log_info "To run the job manually now, execute:"
echo -e "${CYAN}  gcloud run jobs execute $JOB_NAME --region $REGION${NC}"
