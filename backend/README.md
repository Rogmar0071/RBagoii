# UI Blueprint Backend

FastAPI service that receives Android screen-recording uploads, runs the
`ui_blueprint` extractor + preview generator in a background thread, and
exposes the results over HTTP.

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/sessions` | Upload a clip (`video` MP4 + optional `meta` JSON) |
| `GET`  | `/v1/sessions/{id}` | Poll extraction status |
| `GET`  | `/v1/sessions/{id}/blueprint` | Download blueprint JSON |
| `GET`  | `/v1/sessions/{id}/preview/index` | List preview PNG filenames |
| `GET`  | `/v1/sessions/{id}/preview/{file}` | Download a preview PNG |

All endpoints require `Authorization: Bearer <API_KEY>`.

---

## Local development

```bash
# From repo root
pip install ".[video]"
pip install -r backend/requirements.txt

API_KEY=dev-secret uvicorn backend.app.main:app --reload
```

---

## Docker Compose (local smoke test)

```bash
# From repo root
API_KEY=my-secret docker compose up --build
```

Upload a clip:

```bash
curl -X POST http://localhost:8000/v1/sessions \
  -H "Authorization: Bearer my-secret" \
  -F "video=@/path/to/recording.mp4" \
  -F 'meta={"device":"Pixel 8","fps":30}'
# → {"session_id":"<uuid>","status":"queued"}

# Poll status
curl http://localhost:8000/v1/sessions/<uuid> \
  -H "Authorization: Bearer my-secret"
```

---

## Oracle Free Tier deployment

These steps assume a fresh **Oracle Linux 8** (or Ubuntu 22.04) VM with 1 OCPU / 1 GB RAM from the Oracle Always-Free tier.

### 1 — Provision the VM

1. Log in to <https://cloud.oracle.com> → Compute → Instances → **Create Instance**.
2. Choose **VM.Standard.A1.Flex** (Ampere, Always-Free) or **VM.Standard.E2.1.Micro**.
3. Select Oracle Linux 8 or Canonical Ubuntu 22.04 image.
4. Add your SSH public key and note the public IP.

### 2 — Install Docker

**Ubuntu:**
```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER
newgrp docker
```

**Oracle Linux:**
```bash
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

### 3 — Open firewall port 8000

In the OCI Console: **Networking → Virtual Cloud Networks → your VCN → Security Lists → Ingress Rules → Add Ingress Rule**:
- Source CIDR: `0.0.0.0/0`
- Destination port: `8000`
- Protocol: TCP

Also open in the OS firewall:
```bash
# Oracle Linux
sudo firewall-cmd --permanent --add-port=8000/tcp && sudo firewall-cmd --reload

# Ubuntu (if ufw is active)
sudo ufw allow 8000/tcp
```

### 4 — Deploy

```bash
# Clone the repo
git clone https://github.com/Rogmar0071/ui-blueprint.git
cd ui-blueprint

# Set a strong API key
export API_KEY=$(openssl rand -hex 32)
echo "API_KEY=$API_KEY" > .env   # keep this secret

# Build and start
docker compose --env-file .env up -d --build
```

### 5 — Verify

```bash
curl http://<YOUR_VM_IP>:8000/docs
```

The FastAPI Swagger UI should load.  Use `API_KEY` from your `.env` as the bearer token.

### 6 — Persistent data

The `ui_blueprint_data` Docker volume stores all sessions under `/data`.  
Back it up with:

```bash
docker run --rm -v ui_blueprint_data:/data -v $(pwd):/backup alpine \
  tar czf /backup/ui_blueprint_data.tar.gz /data
```

### 7 — (Optional) Reverse proxy with Nginx

To serve over HTTPS, install Nginx + Certbot, configure a proxy_pass to `localhost:8000`, and expose port 443.

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | *(empty — no auth)* | Bearer token required by all endpoints |
| `DATA_DIR` | `./data` | Root directory for session files |
| `BACKEND_DISABLE_JOBS` | `0` | Set to `1` to skip background jobs (tests) |
