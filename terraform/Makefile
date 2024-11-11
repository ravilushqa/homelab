DEFAULT_KUBECONFIG := ~/.kube/config
DEFAULT_GOAL := bootstrap

bootstrap: terraform-init terraform-plan terraform-apply kubeconfig

terraform-init:
	@echo "Initializing Terraform configuration..."
	@terraform init

terraform-plan:
	@echo "Planning Terraform configuration..."
	@terraform plan


terraform-apply:
	@echo "Applying Terraform configuration..."
	@terraform apply -auto-approve

kubeconfig:
	@echo "Fetching kubeconfig from Terraform and setting it as the default..."
	@mkdir -p ~/.kube
	@terraform output -raw kubeconfig_content > $(DEFAULT_KUBECONFIG)
	@echo "KUBECONFIG is set as the default at $(DEFAULT_KUBECONFIG)"