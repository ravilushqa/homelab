gen:
	kubectl create secret generic config-cloudflare-ddns \
      --from-file=config.json \
      --dry-run=client -o yaml -n ddns | \
    kubeseal \
      --controller-namespace sealed-secrets \
      --controller-name sealed-secrets-controller \
      --format yaml > sealed-config.yaml