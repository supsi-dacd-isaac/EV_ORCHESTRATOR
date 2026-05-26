# EV_ORCHESTRATOR

## Docker image

This repository publishes a Docker image to GitHub Container Registry when a GitHub Release is published.

The repository can remain private. The GHCR container package is managed separately and must be set to public from the package settings if GitHub creates it as private initially.

Pull the latest published image with:

```bash
docker pull ghcr.io/supsi-dacd-isaac/ev_orchestrator:latest
```
