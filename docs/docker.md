# Docker, Compose and Swarm

The studio (`folivox ui`) runs in a container and serves the network instead of `127.0.0.1`. The container keeps
everything it needs in one volume, `/data`. Every API call still needs the access token.

| File | What it is |
|---|---|
| [`Dockerfile`](../Dockerfile) | The image: Python 3.10, ffmpeg, Folivox with all extras, PyTorch picked by build argument |
| [`deploy/compose.yml`](../deploy/compose.yml) | One Docker host (`docker compose`) |
| [`deploy/docker-stack.yml`](../deploy/docker-stack.yml) | Docker Swarm (`docker stack deploy`) |
| [`deploy/entrypoint.sh`](../deploy/entrypoint.sh) | Turns Docker secrets into the API-key variables Folivox reads |

## Build the image

```bash
docker build -t folivox .                             # NVIDIA GPU (PyTorch 2.5, CUDA 12.4 inside the wheels)
docker build -t folivox --build-arg TORCH=cpu .       # no GPU: smaller image, slow synthesis
docker build -t folivox --build-arg TORCH=cu128 .     # RTX 50 series (PyTorch 2.7, CUDA 12.8)
```

The GPU image needs no CUDA base image. The PyTorch wheels ship CUDA, and the host only provides the NVIDIA driver
through the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/).
The models are not baked in. The voice model (about 6 GB) and the speech-recognition model (about 1.6 GB) download
into `/data` on the first render, and the studio shows the download's progress. To fetch them up front, run this once
the container is running: `docker exec -it <container> folivox setup`.

## What lives where

| Path in the container | Contents | Keep it on |
|---|---|---|
| `/data` (`HOME`) | models, your voices, studio state, uploaded books, `config.toml`, Hugging Face cache, the studio token | a named volume |
| `/data/Audiobooks` | finished audiobooks (the studio's default output folder) | a bind mount you can reach (NAS share, NFS) |

The container runs as uid 1000. A bind-mounted output folder must be writable by it:
`sudo chown 1000:1000 /srv/folivox/audiobooks`.

Finished audiobooks can also be fetched from the browser with **Download** on the "ready" card. An `.m4b` downloads
as is, and an MP3 folder downloads as a zip. **Show in folder** has no file manager to open in a container.

## Access: token and host names

* **Token.** Set it with `FOLIVOX_UI_TOKEN`, or point `FOLIVOX_UI_TOKEN_FILE` at a file such as a Docker secret. It
  must be at least 16 characters (`openssl rand -base64 24`). Without either, the studio makes one, keeps it in
  `/data`, and prints the sign-in link in the container log. The first visit is `http://<host>:8765/?token=<token>`.
  The browser then keeps a cookie, so you can bookmark `http://<host>:8765/`.
* **Host names.** The studio answers only to names it knows. This guards against DNS rebinding. List the names the
  browser will use in `FOLIVOX_UI_ALLOW_HOSTS`, space-separated, with or without a port. For example:
  `FOLIVOX_UI_ALLOW_HOSTS="folivox.example.lan 192.168.1.20"`. `*` accepts any name, which is the default in the
  deploy files for an easy first start. On a network others can reach, set the real names instead.
* **TLS.** The studio speaks plain HTTP. Beyond your LAN, put it behind a reverse proxy that terminates HTTPS
  (Traefik, Caddy, nginx), and add the proxy's public name to `FOLIVOX_UI_ALLOW_HOSTS`. The progress stream is
  Server-Sent Events, so turn off response buffering for the studio's route (nginx: `proxy_buffering off;`).
* **API keys** for the cloud engines (`OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, `GEMINI_API_KEY`, `CARTESIA_API_KEY`,
  `OPENROUTER_API_KEY`). A container has no OS keychain, so the studio's "save key" button cannot store them there.
  Pass them as environment variables, or as Docker secrets named after the variable. The entrypoint exports
  `/run/secrets/OPENAI_API_KEY` as `OPENAI_API_KEY`.

The same settings exist as `folivox ui` options outside Docker: `--host 0.0.0.0`, `--allow-host NAME` (repeatable),
and `--port`.

## One host: Compose

```bash
docker compose -f deploy/compose.yml up -d --build
docker compose -f deploy/compose.yml logs studio      # the sign-in link with the token
```

`FOLIVOX_TORCH=cpu` builds the CPU image. Without an NVIDIA GPU, also delete the `deploy.resources` block.
`FOLIVOX_OUTPUT` picks the host folder for finished books (default `deploy/audiobooks`).

## Docker Swarm

1. **Build and push** to a registry every node can pull from. `docker stack deploy` does not build.

   ```bash
   docker build -t registry.example.lan/folivox:latest . && docker push registry.example.lan/folivox:latest
   ```

2. **Pick the node.** The studio runs as one replica, because a render runs inside its process and its state lives in
   `/data`. A local volume stays on the node where it was created, so pin the service to one node, ideally the one
   with the GPU:

   ```bash
   docker node update --label-add folivox=true <node>
   ```

   To let the studio move between nodes, put `/data` and the output folder on shared storage, such as an NFS volume.
   Run only one studio against that storage at a time. The stack's `update_config.order: stop-first` makes sure of
   this during updates.

3. **Create the token secret** (and any API-key secrets):

   ```bash
   openssl rand -base64 24 | tr -d '\n' | docker secret create folivox_ui_token -
   printf %s "sk-..." | docker secret create OPENAI_API_KEY -     # optional; then uncomment it in the stack file
   ```

4. **Prepare the output folder** on that node: `sudo mkdir -p /srv/folivox/audiobooks && sudo chown 1000:1000 /srv/folivox/audiobooks`.

5. **Deploy:**

   ```bash
   FOLIVOX_IMAGE=registry.example.lan/folivox:latest \
   FOLIVOX_HOSTS="folivox.example.lan 192.168.1.20" \
     docker stack deploy -c deploy/docker-stack.yml folivox
   docker service logs -f folivox_studio
   ```

   Then open `http://<node>:8765/?token=<the secret's value>` once.

Optional variables: `FOLIVOX_PORT` sets the published port (default 8765). `FOLIVOX_OUTPUT` sets the host folder
for finished books (default `/srv/folivox/audiobooks`).

### GPU on Swarm

Swarm services cannot request GPUs the way Compose does (`devices:`). The GPU node has to advertise its GPU as a
*generic resource*, and the NVIDIA runtime has to be the default. On the GPU node:

1. Install the NVIDIA driver and the NVIDIA Container Toolkit.
2. Find the GPU's UUID: `nvidia-smi -a | grep UUID`.
3. Edit `/etc/docker/daemon.json`:

   ```json
   {
     "default-runtime": "nvidia",
     "runtimes": { "nvidia": { "path": "nvidia-container-runtime", "runtimeArgs": [] } },
     "node-generic-resources": ["NVIDIA-GPU=GPU-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"]
   }
   ```

4. In `/etc/nvidia-container-runtime/config.toml`, uncomment `swarm-resource = "DOCKER_RESOURCE_GPU"`.
5. Restart Docker: `sudo systemctl restart docker`.

The stack reserves `NVIDIA-GPU: 1`, so the studio is placed only on a node that advertises one. For a CPU-only
image, delete the `generic_resources` block. `folivox doctor` inside the container shows whether it sees the GPU:
`docker exec -it $(docker ps -qf name=folivox_studio) folivox doctor`.

### Notes

* **Memory.** The voice model and the speech-recognition check need about 8 GB of RAM (the stack reserves 8G). On
  the GPU, the TTS model needs 4 GB of VRAM. With 8 GB or more, Whisper moves to the GPU as well.
* **Stopping mid-render is safe.** The journal is crash-safe. After `docker service update` or a node restart, render
  the same book again and it continues where it stopped. `stop_grace_period: 60s` gives the render time to save.
* **`/dev/shm`.** Swarm has no `shm_size`, so the stack mounts a 2 GB tmpfs there for PyTorch.
* **Updating.** Rebuild and push the image, then run `docker service update --image registry.example.lan/folivox:latest folivox_studio`.
  Models and voices stay in the volume.
