.ONESHELL: # Applies to every targets in the file!
DEFAULT_KUBECONFIG := ~/.kube/config
.DEFAULT_GOAL := bootstrap

include k8s/infra/network/cloudflare-ddns/Makefile

# Create a new external service configuration
create-external-service:
	@echo "Creating a new external service using components..."
	@echo "Usage: make create-external-service SERVICE=name PORT=port IP=ip"
	@[ -n "$(SERVICE)" ] || { echo "Error: SERVICE parameter is required"; exit 1; }
	@[ -n "$(PORT)" ] || { echo "Error: PORT parameter is required"; exit 1; }
	@[ -n "$(IP)" ] || { echo "Error: IP parameter is required"; exit 1; }
	@./scripts/create-external-service-component.sh "$(SERVICE)" "$(PORT)" "$(IP)"

# Create a new TLS service configuration
create-tls-service:
	@echo "Creating a new TLS service using components..."
	@echo "Usage: make create-tls-service SERVICE=name PORT=port IP=ip"
	@[ -n "$(SERVICE)" ] || { echo "Error: SERVICE parameter is required"; exit 1; }
	@[ -n "$(PORT)" ] || { echo "Error: PORT parameter is required"; exit 1; }
	@[ -n "$(IP)" ] || { echo "Error: IP parameter is required"; exit 1; }
	@./scripts/create-tls-service-component.sh "$(SERVICE)" "$(PORT)" "$(IP)"

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

k8s-apply: cloudflare-ddns-gen
	@echo "Patch the default storage class..."
	kubectl patch storageclass proxmox-csi -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
	@echo "Applying critical bootstrap components..."
	kubectl apply -k ./k8s/infra/crds
	kubectl kustomize --enable-helm ./k8s/infra/network/cilium | kubectl apply -f -
	kubectl kustomize --enable-helm ./k8s/infra/security/cert-manager | kubectl apply -f -
	@echo "Deploying ArgoCD..."
	kubectl kustomize --enable-helm ./k8s/infra/argocd | kubectl apply -f -
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

glance-restart:
	kubectl kustomize ./k8s/apps/internal/glance | kubectl apply -f -
	kubectl delete pod -l app=glance -n glance

cloudflare-ddns-gen:
	#$(MAKE) -C k8s/infra/network/cloudflare-ddns gen

isponsorblocktv-restart:
	kubectl delete pod -l app=isponsorblocktv -n isponsorblocktv
	kubectl kustomize ./k8s/apps/internal/isponsorblocktv | kubectl apply -f -
