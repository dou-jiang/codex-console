# Internal Deployment Guide

This guide is for the minimum internal-service deployment shape of this repository.

## Scope

This is intentionally not a public-internet deployment guide.

Recommended access model:

- localhost only
- SSH tunnel
- jump host
- internal reverse proxy

Do not expose this service directly to the public internet by default.

## Minimum Security Rules

Before starting:

1. Set a strong `APP_ACCESS_PASSWORD`
2. Keep the service bound to localhost unless an internal proxy needs otherwise
3. Do not enable VNC/noVNC unless you explicitly need it
4. If you enable VNC, set `VNC_PASSWORD`

## Required Environment Variables

Minimum recommended runtime configuration:

```bash
APP_HOST=127.0.0.1
APP_PORT=8000
APP_ACCESS_PASSWORD=StrongPass123!
APP_DATABASE_URL=sqlite:///./data/database.db
APP_DATA_DIR=./data
APP_LOGS_DIR=./logs
DEBUG=0
LOG_LEVEL=INFO
ENABLE_VNC=0
```

Notes:

- `APP_ACCESS_PASSWORD` is required in non-debug mode
- weak values such as `admin123` are rejected at startup
- `WEBUI_*` names are still accepted for compatibility, but `APP_*` is the canonical set now

## Local Process Deployment

Example:

```bash
python webui.py --host 127.0.0.1 --port 8000 --access-password StrongPass123!
```

Health check:

```bash
curl http://127.0.0.1:8000/healthz
```

Expected response:

```json
{"ok": true}
```

## SSH Tunnel Access

If the service is running on an internal host:

```bash
ssh -L 8000:127.0.0.1:8000 your-user@your-host
```

Then open:

```text
http://127.0.0.1:8000
```

## Docker Compose Shape

The repository compose file is now aligned for internal-only defaults:

- host port binds to `127.0.0.1`
- VNC/noVNC is off by default
- health check uses `/healthz`

Recommended start:

```bash
export APP_ACCESS_PASSWORD='StrongPass123!'
docker-compose up -d
```

Recommended verify:

```bash
curl http://127.0.0.1:1455/healthz
```

## Optional Desktop Mode

Only use this when browser automation really requires the desktop layer.

Example:

```bash
ENABLE_VNC=1
VNC_PASSWORD=StrongVncPass123!
```

Notes:

- noVNC/VNC should stay off unless needed
- the start script refuses to launch VNC without a password

## Data Layout

Recommended persistent directories:

```text
/opt/codex-console/data
/opt/codex-console/logs
```

Or for a repository-local deployment:

```text
<repo>/data
<repo>/logs
```

## Current Validation Status

Validated in this repository:

- packaged entrypoint points at `src.webui_entry:main`
- startup safety rejects weak access passwords
- health endpoint exists at `/healthz`
- compatibility mapping from `WEBUI_*` to `APP_*` exists

Not validated on this machine:

- live Docker startup
- live `docker compose config`

Reason:

- `docker` is not installed in the current environment
