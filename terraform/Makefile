DEFAULT_KUBECONFIG := ~/.kube/config

terraform-apply:
	@echo "Applying Terraform configuration..."
	@terraform apply -auto-approve

kubeconfig:
	@echo "Fetching kubeconfig from Terraform and setting it as the default..."
	@mkdir -p ~/.kube
	@terraform output -raw kubeconfig_content > $(DEFAULT_KUBECONFIG)
	@echo "KUBECONFIG is set as the default at $(DEFAULT_KUBECONFIG)"