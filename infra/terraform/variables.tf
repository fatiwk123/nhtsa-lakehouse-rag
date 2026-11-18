variable "project" {
  description = "Name prefix of the resources."
  type        = string
  default     = "nhtsa-lakehouse-rag"
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "eu-west-3"
}

variable "instance_type" {
  description = "VM size. 16 GB of RAM is the minimum (Ollama ~3.5 GB, Postgres, MinIO, Dagster, API); more vCPUs = faster LLM answers."
  type        = string
  default     = "m6i.xlarge"
}

variable "root_volume_gb" {
  description = "Root disk size (Docker images ~10 GB, LLM ~2.5 GB, lakehouse + index < 1 GB on the default scope)."
  type        = number
  default     = 40
}

variable "ssh_public_key" {
  description = "Public SSH key allowed to log in as `ubuntu` (content of ~/.ssh/id_ed25519.pub)."
  type        = string
}

variable "allowed_cidrs" {
  description = "CIDR blocks allowed to reach the VM (SSH, and the UI when expose_ui = true), e.g. [\"203.0.113.7/32\"]."
  type        = list(string)

  validation {
    condition     = length(var.allowed_cidrs) > 0 && !contains(var.allowed_cidrs, "0.0.0.0/0")
    error_message = "List at least one CIDR and do not open the VM to the whole internet (0.0.0.0/0)."
  }
}

variable "expose_ui" {
  description = "Open the Streamlit UI (port 8501) to allowed_cidrs. Off by default: use the SSH tunnel from the outputs."
  type        = bool
  default     = false
}

variable "repo_url" {
  description = "Git repository deployed on the VM."
  type        = string
  default     = "https://github.com/fatiwk123/nhtsa-lakehouse-rag.git"
}

variable "repo_ref" {
  description = "Branch or tag to deploy."
  type        = string
  default     = "main"
}

variable "nhtsa_makes" {
  description = "Ingestion scope: makes (NHTSA_MAKES)."
  type        = string
  default     = "VOLKSWAGEN"
}

variable "nhtsa_model_years" {
  description = "Ingestion scope: model years (NHTSA_MODEL_YEARS)."
  type        = string
  default     = "2018-2020"
}

variable "ollama_model" {
  description = "Local LLM pulled into Ollama (OLLAMA_MODEL)."
  type        = string
  default     = "qwen3:4b-instruct"
}
