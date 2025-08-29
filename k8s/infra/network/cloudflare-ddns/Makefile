gen:
	kubectl create secret generic config-cloudflare-ddns \
      --from-file=config.json \
      --dry-run=client -o yaml -n ddns | \
    kubeseal \
      --cert ../../../../terraform/modules/sealed-secrets/certs/sealed-secrets.cert \
      --scope strict \
      --namespace ddns \
      --name config-cloudflare-ddns \
      --controller-namespace sealed-secrets \
      --controller-name sealed-secrets-controller \
      --format yaml > sealed-config.yaml

.PHONY: gen