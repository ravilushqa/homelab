# Immich Migration Plan (.49 → .166)

## Prerequisites
1. Mount RAID1 (`/mnt/raid1`) from Proxmox host to LXC .166
   - Photos at `/mnt/raid1/library` (~800GB used on RAID)
   - Paperless also uses `/mnt/raid1/paperless/`
2. Enough disk space on .166 for Postgres + ML model cache

## Required Komodo Secrets
- `IMMICH_VERSION` = release
- `UPLOAD_LOCATION` = /mnt/raid1/library
- `DB_PASSWORD` = postgres
- `DB_USERNAME` = postgres
- `DB_DATABASE_NAME` = immich

## Migration Steps

### 1. Stop Immich on .49
```bash
ssh claw@192.168.1.49 "cd /opt/stacks/immich && docker compose stop"
```

### 2. Dump Postgres
```bash
ssh claw@192.168.1.49 "docker exec immich_postgres pg_dumpall -U postgres" > /tmp/immich_pg_dump.sql
```

### 3. Deploy Immich on .166 (empty DB)
Deploy via Komodo (will create fresh postgres)

### 4. Restore Postgres dump
```bash
docker exec -i immich_postgres psql -U postgres < /tmp/immich_pg_dump.sql
```

### 5. Restart Immich
```bash
# Via Komodo API: DeployStack immich
```

### 6. Verify
- Check https://immich.ravil.space
- Verify photos are accessible

### 7. Update routing
- Add `immich.ravil.space` to Komodo tcp_routers.yaml
- Remove K8s external service for immich

## Notes
- DB is ~477MB, manageable
- Photos don't need copying — same RAID mount
- Machine learning model cache will re-download (~2-3GB)
- Immich should bypass OIDC middleware (traefik.http.routers.immich.middlewares= empty)
