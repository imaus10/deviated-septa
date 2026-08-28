variable "environment" {
  description = "Environment name: dev or prod"
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be 'dev' or 'prod'."
  }
}

variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "alert_email" {
  description = "Email address for AWS billing budget alerts"
  type        = string
}

variable "budget_limit" {
  description = "Monthly cost budget ceiling (USD)"
  type        = number
  default     = 20
}