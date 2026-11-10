output "cloud_run_service_uri" {
  description = "Authenticated HTTPS endpoint for TripPilot ADK API Server."
  value       = google_cloud_run_v2_service.trippilot.uri
}

output "runtime_service_account_email" {
  description = "Least-privilege runtime Service Account email."
  value       = google_service_account.agent_runtime.email
}

output "artifact_registry_repository" {
  description = "Artifact Registry Docker repository ID."
  value       = google_artifact_registry_repository.agent_images.name
}
