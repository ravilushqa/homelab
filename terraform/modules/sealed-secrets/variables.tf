variable "helm_values" {
  description = "Values for the Helm chart"
  type        = string
}

variable "cert" {
  type = object({
    cert = string
    key = string
  })
}