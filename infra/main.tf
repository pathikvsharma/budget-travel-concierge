terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  required_apis = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudtrace.googleapis.com",
    "logging.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
  ])
}

resource "google_project_service" "enabled_apis" {
  for_each           = local.required_apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# Dedicated least-privilege runtime identity (no default Compute SA).
resource "google_service_account" "agent_runtime" {
  account_id   = "trippilot-agent-sa"
  display_name = "TripPilot ADK Concierge Runtime SA"
  depends_on   = [google_project_service.enabled_apis]
}

locals {
  runtime_roles = toset([
    "roles/aiplatform.user",
    "roles/cloudtrace.agent",
    "roles/logging.logWriter",
  ])
}

resource "google_project_iam_member" "runtime_least_privilege" {
  for_each = local.runtime_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.agent_runtime.email}"
}

resource "google_artifact_registry_repository" "agent_images" {
  location      = var.region
  repository_id = "trippilot-agents"
  description   = "Container images for TripPilot ADK Travel Concierge"
  format        = "DOCKER"
  depends_on    = [google_project_service.enabled_apis]
}

# Managed Secret placeholder (optional fallback when not exclusively using Vertex AI ADC).
resource "google_secret_manager_secret" "session_db_url" {
  secret_id = "${var.service_name}-session-db-url"
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled_apis]
}

resource "google_secret_manager_secret_iam_member" "session_db_accessor" {
  secret_id = google_secret_manager_secret.session_db_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_runtime.email}"
}

# Authenticated-only Cloud Run v2 deployment (IAM invoker gated by default; fail-closed).
resource "google_cloud_run_v2_service" "trippilot" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = google_service_account.agent_runtime.email
    max_instance_request_concurrency = 20
    timeout                          = "180s"

    scaling {
      min_instance_count = 0
      max_instance_count = 5
    }

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "1Gi"
        }
      }

      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "TRUE"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.vertex_ai_location
      }
      env {
        name  = "FAST_MODEL"
        value = var.fast_model
      }
      env {
        name  = "REASONING_MODEL"
        value = var.reasoning_model
      }
      env {
        name  = "ENABLE_CLOUD_TRACE"
        value = "TRUE"
      }
      env {
        name  = "HOST"
        value = "0.0.0.0"
      }
      env {
        name = "TRAVEL_SESSION_DB_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.session_db_url.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_iam_member.runtime_least_privilege,
    google_secret_manager_secret_iam_member.session_db_accessor,
  ]
}
