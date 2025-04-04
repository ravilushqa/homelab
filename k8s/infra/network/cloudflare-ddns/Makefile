fetch-cert:
	kubeseal --fetch-cert \
      --controller-namespace sealed-secrets \
      --controller-name sealed-secrets-controller \
      > sealed-secrets-public-key.pem

gen: fetch-cert
	kubectl create secret generic config-cloudflare-ddns \
      --from-file=config.json \
      --dry-run=client -o yaml -n ddns | \
    kubeseal \
      --cert sealed-secrets-public-key.pem \
      --scope strict \
      --namespace ddns \
      --name config-cloudflare-ddns \
      --controller-namespace sealed-secrets \
      --controller-name sealed-secrets-controller \
      --format yaml > sealed-config.yaml

.PHONY: fetch-cert gen