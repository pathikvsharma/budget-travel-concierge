variable "project_id" {
  description = "Google Cloud Project ID hosting TripPilot."
  type        = string
}

variable "region" {
  description = "Google Cloud region for Cloud Run, Artifact Registry, and regional resources."
  type        = string
  default     = "us-central1"
}

variable "vertex_ai_location" {
  description = "Vertex AI endpoint location for Gemini calls. Gemini 3.x models are served from 'global'."
  type        = string
  default     = "global"
}

variable "service_name" {
  description = "Cloud Run v2 service name."
  type        = string
  default     = "trippilot-travel-concierge"
}

variable "container_image" {
  description = "Immutable Artifact Registry image digest or tag to deploy."
  type        = string
}

variable "fast_model" {
  description = "Low-latency Gemini model tier for routing and parallel research."
  type        = string
  default     = "gemini-3.5-flash"
}

variable "reasoning_model" {
  description = "Deep-reasoning Gemini model tier for multi-constraint itinerary planning."
  type        = string
  default     = "gemini-3.1-pro-preview"
}

variable "use_session_db_secret" {
  description = "Read TRAVEL_SESSION_DB_URL (e.g. a Cloud SQL Postgres URL) from Secret Manager. Add a secret version before enabling."
  type        = bool
  default     = false
}
