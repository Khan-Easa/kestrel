# Publishing & Deploying Kestrel

This guide covers two operator tasks that touch external accounts: **publishing the
images to Docker Hub** and **deploying a public demo**. The commands here are meant
to be run by you, with your own credentials and cloud account — Kestrel ships the
recipe, you press the buttons.

For local/self-host setup see [`deployment.md`](deployment.md); this doc is about
making Kestrel available to *others*.

## Part 1 — Publish images to Docker Hub

Kestrel builds two images:

- `kestrel-runtime` — the sandbox the user code runs in.
- `kestrel-api` — the API control plane.

Publishing them lets people `docker pull` instead of building from source. Replace
`YOURNAME` with your Docker Hub username throughout.

```bash
# 1. Build both images locally (if not already built).
docker build -t kestrel-runtime:0.5.0 docker/executor/
docker build -f docker/api/Dockerfile -t kestrel-api:0.8.0 .

# 2. Log in to Docker Hub.
docker login

# 3. Tag for your namespace — both the version and a moving `latest`.
docker tag kestrel-runtime:0.5.0 YOURNAME/kestrel-runtime:0.5.0
docker tag kestrel-runtime:0.5.0 YOURNAME/kestrel-runtime:latest
docker tag kestrel-api:0.8.0     YOURNAME/kestrel-api:0.8.0
docker tag kestrel-api:0.8.0     YOURNAME/kestrel-api:latest

# 4. Push.
docker push YOURNAME/kestrel-runtime:0.5.0
docker push YOURNAME/kestrel-runtime:latest
docker push YOURNAME/kestrel-api:0.8.0
docker push YOURNAME/kestrel-api:latest
```

**Versioning discipline:** bump the image tag whenever its contents change — the
runtime image tracks the kernel/deps (currently `0.5.0`), the API image tracks the
release (`0.8.0`). Never overwrite an existing version tag with different contents;
`latest` is the only moving tag.

**README badge:** once the API image is pushed, add a Docker-pulls badge near the
top of `README.md`:

```markdown
[![Docker pulls](https://img.shields.io/docker/pulls/YOURNAME/kestrel-api.svg)](https://hub.docker.com/r/YOURNAME/kestrel-api)
```

(This is one of the badges deliberately deferred until publishing makes it real.)

## Part 2 — Deploy a public demo

### The constraint that picks the platform

Kestrel launches sandbox containers by talking to a **host Docker daemon** it
controls (docker-out-of-docker — see [`deployment.md`](deployment.md)). That rules
out most "git push" app platforms:

- **Railway / Render / Heroku-style PaaS** run *your* container but don't give you
  the host Docker socket — the API can't spawn sandboxes there (it would fall back
  to the insecure `subprocess` backend, defeating the purpose). **Not suitable.**
- **Fly.io / serverless-container platforms** can be made to work but need Docker
  running *inside* the unit, which is fiddly.
- **A plain cloud VM you control** (DigitalOcean droplet, Hetzner, AWS EC2, GCP
  Compute, a Fly Machine in VM mode) — has a real Docker daemon, runs our Compose
  stack unchanged. **This is the recommended target**, and it matches Kestrel's
  single-node design.

### Recipe: VM + Docker Compose

On a fresh small Linux VM (2 vCPU / 2–4 GB is plenty for a demo) with Docker and the
Compose plugin installed:

```bash
# 1. Get the code.
git clone https://github.com/Khan-Easa/kestrel.git && cd kestrel

# 2. Build the sandbox runtime image on the host daemon.
docker build -t kestrel-runtime:0.5.0 docker/executor/

# 3. Bring up the stack (API + Redis + Postgres).
docker compose up -d --build

# 4. Mint a key for whoever will use the demo.
docker compose exec api kestrel-keys create demo --scope execute
```

(To run from your published images instead of building, set the `api` service's
`image:` to `YOURNAME/kestrel-api:0.8.0` and drop its `build:` block, then
`docker compose pull` — but build-from-source is simplest for a demo.)

### Locking down a public demo

A public Kestrel is an internet-facing endpoint that runs code. Before exposing it:

- **Auth must be ON.** Leave `KESTREL_DEV_API_KEY` empty and rely on the Postgres key
  store (the Compose default). Hand out scoped keys; never publish an admin key.
- **Keep the rate limits low** for a shared demo key (set `KESTREL_RATE_LIMIT_EXECUTE_PER_MINUTE`
  conservatively).
- **Put TLS in front.** Run a reverse proxy (Caddy gets you automatic HTTPS in a few
  lines, or use nginx + certbot) terminating TLS and forwarding to the API's port
  8000. Don't expose 8000 directly.
- **Firewall everything except 80/443.** Postgres (5432) and Redis (6379) must not be
  reachable from the internet — the Compose stack already keeps them off published
  ports; don't add port mappings for them.
- **Set `KESTREL_LOG_JSON=true`** and scrape `/metrics`.
- **Expect abuse.** The sandbox is hardened (no network, capped, throwaway), which is
  the whole point — but watch resource usage and `kestrel_audit_dropped_total`.

### Demo limitations to document for visitors

- Containers have **no network**, so code can't `pip install` or fetch URLs.
- Tight CPU/memory/time limits; long or heavy jobs are killed.
- Sessions and data are **ephemeral** — nothing persists.

## What stays your call

This file is the recipe. The acts that touch your accounts — `docker login`/`push`,
provisioning the VM, pointing a domain, and keeping the demo running — are yours to
run when you choose. The repository is fully publishable and deployable as of this
substep; whether and when it goes live is up to you.
