# UI Blueprint Backend

FastAPI service that receives Android screen-recording uploads, runs the
`ui_blueprint` extractor + preview generator in a background thread, and
exposes the results over HTTP.  Also exposes a clip-analysis endpoint that
uses OpenAI to return structured insights about a recorded clip.

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/sessions` | Upload a clip (`video` MP4 + optional `meta` JSON) |
| `GET`  | `/v1/sessions/{id}` | Poll extraction status |
| `GET`  | `/v1/sessions/{id}/blueprint` | Download blueprint JSON |
| `GET`  | `/v1/sessions/{id}/preview/index` | List preview PNG filenames |
| `GET`  | `/v1/sessions/{id}/preview/{file}` | Download a preview PNG |
| `POST` | `/v1/analyze` | Analyze a clip with OpenAI (see below) |

All `/v1/sessions` endpoints require `Authorization: Bearer <API_KEY>`.  
`/v1/analyze` does **not** require auth beyond `OPENAI_API_KEY` being set in the environment.

---

## POST /v1/analyze

Accepts a multipart upload, extracts frames + optionally transcribes audio, then
calls the OpenAI Responses API and returns structured conclusions.
**The video is never stored permanently** — it is processed inside a temporary
directory that is deleted immediately after the response is sent.

### Request

```
Content-Type: multipart/form-data

video        – MP4 file
requirements – string (e.g. "Identify any safety hazards")
```

### Response JSON

```json
{
  "summary": "The clip shows a conveyor belt operating normally...",
  "conclusions": [
    "Belt speed appears consistent.",
    "No visible obstructions detected."
  ],
  "key_events": [
    {"t_sec": 2.0, "event": "Object enters frame from left"},
    {"t_sec": 7.0, "event": "Belt briefly slows"}
  ],
  "confidence": 0.82,
  "diagnostics": {
    "frames_used": 12,
    "transcript_used": false,
    "audio_present": false
  }
}
```

### Sample curl

```bash
curl -X POST http://localhost:8000/v1/analyze \
  -F "video=@/path/to/recording.mp4" \
  -F "requirements=Identify any safety hazards visible in the recording"
# → {"summary":"...","conclusions":[...],"key_events":[...],"confidence":0.82,"diagnostics":{...}}
```

---

## Local development

```bash
# From repo root
pip install ".[video]"
pip install -r backend/requirements.txt

API_KEY=dev-secret OPENAI_API_KEY=sk-... uvicorn backend.app.main:app --reload
```

---

## Docker Compose (local smoke test)

```bash
# From repo root
API_KEY=my-secret OPENAI_API_KEY=sk-... docker compose up --build
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

# Analyze a clip
curl -X POST http://localhost:8000/v1/analyze \
  -F "video=@/path/to/recording.mp4" \
  -F "requirements=Describe the main activity in the recording"
```

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | *(empty — no auth)* | Bearer token required by `/v1/sessions` endpoints |
| `DATA_DIR` | `./data` | Root directory for session files |
| `OPENAI_API_KEY` | *(required for /v1/analyze)* | OpenAI API key |
| `BACKEND_DISABLE_JOBS` | `0` | Set to `1` to skip background jobs (tests) |

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
| `API_KEY` | *(empty — no auth)* | Bearer token required by `/v1/sessions` endpoints |
| `DATA_DIR` | `./data` | Root directory for session files |
| `OPENAI_API_KEY` | *(required for /v1/analyze)* | OpenAI API key |
| `BACKEND_DISABLE_JOBS` | `0` | Set to `1` to skip background jobs (tests) |
