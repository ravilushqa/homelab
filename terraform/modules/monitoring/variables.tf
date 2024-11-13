variable "enabled" {
  type    = bool
  default = false
}

variable "namespace" {
  type    = string
  default = "monitoring"
}
variable "cluster_name" {
  type = string
}

variable "externalservices_prometheus_host" {
  type = string
}
variable "externalservices_prometheus_basicauth_username" {
  type = number
}
variable "externalservices_prometheus_basicauth_password" {
  type = string
}
variable "externalservices_loki_host" {
  type = string
}
variable "externalservices_loki_basicauth_username" {
  type = number
}
variable "externalservices_loki_basicauth_password" {
  type = string
}
variable "externalservices_tempo_host" {
  type = string
}
variable "externalservices_tempo_basicauth_username" {
  type = number
}
variable "externalservices_tempo_basicauth_password" {
  type = string
}
