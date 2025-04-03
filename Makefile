.ONESHELL: # Applies to every targets in the file!
DEFAULT_KUBECONFIG := ~/.kube/config
DEFAULT_GOAL := bootstrap

include k8s/infra/network/cloudflare-ddns/Makefile

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
	@echo "Applying Kubernetes configuration..."
	kubectl apply -k ./k8s/infra/crds
	kubectl kustomize --enable-helm ./k8s/infra/network/cilium | kubectl apply -f -
	kubectl kustomize --enable-helm ./k8s/infra/security/cert-manager | kubectl apply -f -
	kubectl kustomize ./k8s/infra/network/gateway | kubectl apply -f -
	kubectl kustomize ./k8s/infra/network/cloudflare-ddns | kubectl apply -f -
	kubectl kustomize ./k8s/apps/external/proxmox | kubectl apply -f -
	kubectl kustomize ./k8s/apps/external/haos | kubectl apply -f -
	kubectl kustomize ./k8s/apps/external/immich | kubectl apply -f -
	kubectl kustomize ./k8s/apps/external/openwebui | kubectl apply -f -
	kubectl kustomize ./k8s/apps/external/grafana | kubectl apply -f -
	kubectl kustomize ./k8s/apps/external/n8n | kubectl apply -f -
	kubectl kustomize ./k8s/apps/external/dockge | kubectl apply -f -
	kubectl kustomize ./k8s/apps/internal/hoarder | kubectl apply -f -
	kubectl kustomize ./k8s/apps/internal/glance | kubectl apply -f -
	kubectl kustomize ./k8s/apps/internal/isponsorblocktv | kubectl apply -f -
	kubectl kustomize ./k8s/apps/external/changedetection | kubectl apply -f -
	#kubectl apply -f ./k8s/infra/network/testing/net-utils-pod.yaml

glance-restart:
	kubectl delete pod -l app=glance -n glance
	kubectl kustomize ./k8s/apps/internal/glance | kubectl apply -f -

cloudflare-ddns-gen:
	$(MAKE) -C k8s/infra/network/cloudflare-ddns gen

isponsorblocktv-restart:
	kubectl delete pod -l app=isponsorblocktv -n isponsorblocktv
	kubectl kustomize ./k8s/apps/internal/isponsorblocktv | kubectl apply -f -
