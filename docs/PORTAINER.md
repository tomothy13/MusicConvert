Portainer Container Setup for MusicConvert

This guide shows how to run MusicConvert in a container under Portainer with minimal effort.

Prerequisites
- A Linux host with Docker and Portainer installed.
- Basic familiarity with Portainer or Docker Compose.

Steps (Docker Compose recommended)

1. Create a project directory on the host and copy the repository there (or mount the repo as a volume).

2. Create a `docker-compose.yml` with the following content:

```yaml
version: '3.7'
services:
  musicconvert:
    image: python:3.11-slim
    container_name: musicconvert
    restart: unless-stopped
    volumes:
      - ./MusicConvert:/app:rw
      - /path/to/music/library:/music:rw
    working_dir: /app
    environment:
      - WEB_HOST=0.0.0.0
      - WEB_PORT=8000
      # Optional: set an admin password to enable admin console
      - ADMIN_PASSWORD=supersecret
    ports:
      - "8000:8000"
    command: ["/bin/sh","-c","python -m pip install --upgrade pip && pip install -r requirements.txt && python web.py"]
```

Notes:
- The compose file mounts the repository so you can edit files directly and keep persistent job outputs under `./MusicConvert/web_output`.
- `/path/to/music/library` maps your host music folder into the container; adjust as needed.

3. Deploy via Portainer
- In Portainer, create a new Stack and paste the `docker-compose.yml` content, or create a container with equivalent settings.

4. Adjust permissions
- The container runs as root by default; for production, consider adding a non-root user and mapping UID/GID for mounted volumes.

5. Optional: Use a lightweight base image
- Build a small image that pre-installs dependencies and includes the code for faster startup. See `Dockerfile` example below.

Example Dockerfile (optional)

```Dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . /app
RUN pip install --upgrade pip && pip install -r requirements.txt
EXPOSE 8000
CMD ["python","web.py"]
```

Security & Networking
- By default the service binds to `0.0.0.0`; Portainer will expose the service externally depending on your host firewall and published ports. To restrict to LAN only, configure Docker publishing rules or host firewall rules.

Troubleshooting
- If the container exits immediately, inspect logs in Portainer or run `docker logs musicconvert`.
- Ensure `ffmpeg` is available in the container: consider installing `ffmpeg` via apt in the Dockerfile if needed.

That's it — you can now deploy the service in Portainer and access the UI at `http://<host>:8000`.
