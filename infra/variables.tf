variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "project" {
  type        = string
  default     = "thaq"
  description = "Prefix for resource names. The SSM parameter names and the workgroup use it too."
}

variable "alert_email" {
  type        = string
  description = "Email address for pipeline alerts. AWS sends a confirmation link first."
}

variable "github_repo" {
  type        = string
  description = "GitHub repository as owner/name. Only its main branch may assume the dashboard role."

  validation {
    condition     = can(regex("^[\\w.-]+/[\\w.-]+$", var.github_repo))
    error_message = "github_repo must look like owner/name."
  }
}

variable "schedule_enabled" {
  type        = bool
  default     = false
  description = "Run the pipeline every day at 09:30 Bangkok time, and alarm when no run succeeds."
}
