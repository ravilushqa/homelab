.ONESHELL: # Applies to every targets in the file!
DEFAULT_KUBECONFIG := ~/.kube/config
.DEFAULT_GOAL := bootstrap

bootstrap: terraform-init terraform-plan terraform-apply kubeconfig k8s-apply

terraform-init:
	@echo "Initializing Terraform configuration..."
	@terraform -chdir=./terraform init

terraform-plan:
	@echo "Planning Terraform configuration..."
	@terraform -chdir=./terraform plan

terraform-apply:
	@echo "Applying Terraform configuration..."
	@terraform -chdir=./terraform apply -auto-approve

kubeconfig:
	@echo "Fetching kubeconfig from Terraform and setting it as the default..."
	@echo "Do you want to set the kubeconfig as the default at $(DEFAULT_KUBECONFIG)? [y/N] " && read ans && [ $${ans:-N} = y ]
	@mkdir -p ~/.kube
	@terraform -chdir=./terraform output -raw kubeconfig_content > $(DEFAULT_KUBECONFIG)
	@echo "KUBECONFIG is set as the default at $(DEFAULT_KUBECONFIG)"

k8s-apply:
	@echo "Applying critical bootstrap components..."
	kubectl apply -k ./k8s/infra/crds
	kubectl kustomize --enable-helm ./k8s/infra/network/cilium | kubectl apply -f -
	kubectl kustomize --enable-helm ./k8s/infra/security/cert-manager | kubectl apply -f -
	@echo "Deploying ArgoCD..."
	kubectl kustomize --enable-helm ./k8s/infra/argocd | kubectl apply -f -
	@echo "Waiting for ArgoCD CRDs to be ready..."
	@sleep 5
	@echo "Applying ArgoCD applications..."
	kubectl apply -k ./k8s/infra/argocd/applications
	@echo "Bootstrap complete. Further application deployments will be managed by ArgoCD."

# ArgoCD management commands
argocd-password:
	@echo "ArgoCD admin password:"
	@kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
	@echo

argocd-restart:
	@echo "Restarting ArgoCD components..."
	kubectl -n argocd delete pod -l app.kubernetes.io/part-of=argocd
	kubectl kustomize --enable-helm ./k8s/infra/argocd | kubectl apply -f -

