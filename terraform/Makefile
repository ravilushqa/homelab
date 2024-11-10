DEFAULT_KUBECONFIG := ~/.kube/config

kubeconfig:
	@echo "Fetching kubeconfig from Terraform and setting it as the default..."
	@mkdir -p ~/.kube
	@terraform output -raw kubeconfig_content > $(DEFAULT_KUBECONFIG)
	@echo "KUBECONFIG is set as the default at $(DEFAULT_KUBECONFIG)"