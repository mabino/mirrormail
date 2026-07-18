#!/usr/bin/env zsh
# ==============================================================================
# deploy_gcp.sh - Bootstrap GCP Resources & Setup Secretless GitOps Flow
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

# Detect GitHub Repository
log_info "Detecting GitHub repository remote..."
GITHUB_REPO=$(git remote get-url origin 2>/dev/null | sed -E 's/.*github.com[:\/]([^.]+)(\.git)?/\1/p')
if [ -z "$GITHUB_REPO" ]; then
    log_warn "Could not detect GitHub repository from git remote."
    read "GITHUB_REPO?Enter your GitHub Repository (e.g. username/repo): "
    if [ -z "$GITHUB_REPO" ]; then
        log_error "GitHub repository is required to setup workload identity federation."
        exit 1
    fi
fi
log_success "GitHub Repository detected: $GITHUB_REPO"

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
log_info "Enabling required Google APIs (Artifact Registry, Cloud Run, Cloud Scheduler, IAM)..."
gcloud services enable \
    artifactregistry.googleapis.com \
    run.googleapis.com \
    cloudscheduler.googleapis.com \
    iam.googleapis.com \
    iamcredentials.googleapis.com > /dev/null
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

# 5. Create GCS Bucket for persistence
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

# 6. Configure Workload Identity Federation & Service Account for GitHub Actions
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")

log_info "Verifying Workload Identity Pool 'mirrormail-pool'..."
if ! gcloud iam workload-identity-pools describe "mirrormail-pool" --location="global" &> /dev/null; then
    log_info "Creating Workload Identity Pool 'mirrormail-pool'..."
    gcloud iam workload-identity-pools create "mirrormail-pool" \
        --location="global" \
        --display-name="Mirrormail GitOps Pool" > /dev/null
else
    log_success "Workload Identity Pool already exists."
fi

log_info "Verifying OIDC Provider 'mirrormail-github'..."
if ! gcloud iam workload-identity-pools providers describe "mirrormail-github" --workload-identity-pool="mirrormail-pool" --location="global" &> /dev/null; then
    log_info "Creating OIDC Provider 'mirrormail-github'..."
    gcloud iam workload-identity-pools providers create-oidc "mirrormail-github" \
        --location="global" \
        --workload-identity-pool="mirrormail-pool" \
        --display-name="GitHub Actions Provider" \
        --attribute-mapping="google.subject=assertion.subject,attribute.repository=assertion.repository" \
        --issuer-uri="https://token.actions.githubusercontent.com" > /dev/null
else
    log_success "OIDC Provider already exists."
fi

log_info "Verifying Service Account 'mirrormail-github-sa'..."
if ! gcloud iam service-accounts describe "mirrormail-github-sa@$PROJECT_ID.iam.gserviceaccount.com" &> /dev/null; then
    log_info "Creating Service Account 'mirrormail-github-sa'..."
    gcloud iam service-accounts create "mirrormail-github-sa" \
        --display-name="GitHub Actions Deploy Service Account" > /dev/null
else
    log_success "Service Account already exists."
fi

log_info "Assigning Roles to Service Account..."
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:mirrormail-github-sa@$PROJECT_ID.iam.gserviceaccount.com" --role="roles/run.developer" > /dev/null 2>&1
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:mirrormail-github-sa@$PROJECT_ID.iam.gserviceaccount.com" --role="roles/artifactregistry.writer" > /dev/null 2>&1
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:mirrormail-github-sa@$PROJECT_ID.iam.gserviceaccount.com" --role="roles/cloudscheduler.admin" > /dev/null 2>&1
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:mirrormail-github-sa@$PROJECT_ID.iam.gserviceaccount.com" --role="roles/storage.objectUser" > /dev/null 2>&1
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:mirrormail-github-sa@$PROJECT_ID.iam.gserviceaccount.com" --role="roles/iam.serviceAccountUser" > /dev/null 2>&1
log_success "Roles assigned successfully."

log_info "Binding GitHub repository OIDC claims to Service Account..."
gcloud iam service-accounts add-iam-policy-binding "mirrormail-github-sa@$PROJECT_ID.iam.gserviceaccount.com" \
    --role="roles/iam.workloadIdentityUser" \
    --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/mirrormail-pool/attribute.repository/$GITHUB_REPO" > /dev/null 2>&1
log_success "Federated provider bound to Service Account."

# 7. Generate GitHub Actions Workflow Files
log_info "Generating GitOps GitHub Actions workflow files..."
mkdir -p .github/workflows

# Deploy Workflow
cat << EOF > .github/workflows/deploy-gcp.yml
name: Deploy to GCP (GitOps)
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

    - name: Google Auth
      uses: google-github-actions/auth@v1
      with:
        workload_identity_provider: 'projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/mirrormail-pool/providers/mirrormail-github'
        service_account: 'mirrormail-github-sa@$PROJECT_ID.iam.gserviceaccount.com'

    - name: Set up Cloud SDK
      uses: google-github-actions/setup-gcloud@v1

    - name: Build and Push to Artifact Registry
      run: |
        gcloud auth configure-docker $REGION-docker.pkg.dev --quiet
        docker build -t $REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/mirrormail:latest .
        docker push $REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/mirrormail:latest

    - name: Deploy Cloud Run Job
      run: |
        gcloud run jobs delete $JOB_NAME --region=$REGION --quiet || true
        
        gcloud run jobs deploy $JOB_NAME \
            --image $REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/mirrormail:latest \
            --region $REGION \
            --command "python3" \
            --args "bridge_daemon.py,--config,/app/data/config.json,--one-shot" \
            --add-volume="name=data-vol,type=cloud-storage,bucket=$BUCKET_NAME" \
            --add-volume-mount="volume=data-vol,mount-path=/app/data"

    - name: Configure Cloud Scheduler Trigger
      run: |
        gcloud scheduler jobs delete $JOB_NAME-trigger --location=$REGION --quiet || true
        
        gcloud scheduler jobs create http $JOB_NAME-trigger \
            --schedule="*/5 * * * *" \
            --location="$REGION" \
            --uri="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/$JOB_NAME:run" \
            --http-method=POST \
            --oauth-service-account-email="mirrormail-github-sa@$PROJECT_ID.iam.gserviceaccount.com"
EOF

# Teardown Workflow
cat << EOF > .github/workflows/teardown-gcp.yml
name: Teardown GCP Resources
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

    - name: Google Auth
      uses: google-github-actions/auth@v1
      with:
        workload_identity_provider: 'projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/mirrormail-pool/providers/mirrormail-github'
        service_account: 'mirrormail-github-sa@$PROJECT_ID.iam.gserviceaccount.com'

    - name: Set up Cloud SDK
      uses: google-github-actions/setup-gcloud@v1

    - name: Delete Cloud Scheduler Trigger
      run: |
        gcloud scheduler jobs delete $JOB_NAME-trigger --location=$REGION --quiet || true

    - name: Delete Cloud Run Job
      run: |
        gcloud run jobs delete $JOB_NAME --region=$REGION --quiet || true

    - name: Delete Cloud Storage Bucket
      run: |
        gcloud storage buckets delete gs://$BUCKET_NAME --quiet || true

    - name: Delete Artifact Registry Repository
      run: |
        gcloud artifacts repositories delete $REPO_NAME --location=$REGION --quiet || true
EOF

log_success "GitHub Actions workflow files created successfully!"
echo -e "  -> .github/workflows/deploy-gcp.yml"
echo -e "  -> .github/workflows/teardown-gcp.yml"

log_info "Bootstrap process complete. To deploy the service:"
echo -e "${YELLOW}1. Commit and push the generated workflow files to GitHub:${NC}"
echo -e "   git add .github/workflows/"
echo -e "   git commit -m 'Configure secretless GitOps GCP deployment workflow'"
echo -e "   git push origin main"
echo -e "${YELLOW}2. Visit your GitHub Actions tab to view the progress of the first deploy!${NC}"
