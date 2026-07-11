# Installation guide

The [README quick start](../README.md#quick-start) is enough for a normal installation. This page covers the decisions and checks that are useful when deploying on a real media server.

## 1. Choose a deployment

| Deployment | Command | Use it when |
| --- | --- | --- |
| Packaged CPU | `docker compose -f docker-compose.ghcr.yml up -d` | Recommended first installation. Uses the published GHCR image. |
| Source CPU | `docker compose up -d` | You want the checked-out Python files mounted directly. |
| Packaged NVIDIA | `docker compose -f docker-compose.gpu.yml up -d` | Docker has access to an NVIDIA GPU through NVIDIA Container Toolkit. |

The public default is CPU `medium`, `int8`, four threads, one transcription, and a 10 GB memory limit. Read the [hardware guide](../README.md#model-and-hardware-guide) before changing the model.

## 2. Clone and configure

```bash
git clone https://github.com/Herbertmt978/Subgen-English-Plex.git
cd Subgen-English-Plex
cp .env.example .env
mkdir -p ./models
```

At minimum, edit these values in `.env`:

```dotenv
MEDIA_ROOT=/srv/media
SUBGEN_MODEL_PATH=./models
TRANSCRIBE_FOLDERS=/media/Movies|/media/TV
PUID=1000
PGID=1000
```

`MEDIA_ROOT` is a host path. It is mounted at `/media` inside the container, so `TRANSCRIBE_FOLDERS` must use paths beneath `/media`.

For example:

| Host folder | Container folder |
| --- | --- |
| `/srv/media/Movies` | `/media/Movies` |
| `/srv/media/TV` | `/media/TV` |

Only mount a root that contains libraries Subgen is allowed to read and modify.

## 3. Check permissions

Find the user and group that own the media:

```bash
id
stat -c '%u:%g %n' /srv/media
```

Put the appropriate numeric IDs in `.env`. The container needs read access to media and write access to the directory where each `.srt` will be created.

## 4. Validate before starting

```bash
docker compose -f docker-compose.ghcr.yml config --quiet
```

For NVIDIA:

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.8.0-base-ubuntu24.04 nvidia-smi
docker compose -f docker-compose.gpu.yml config --quiet
```

The CUDA test image version is only a connectivity check; Subgen supplies its own runtime image.

## 5. Start and verify

Packaged CPU:

```bash
docker compose -f docker-compose.ghcr.yml up -d
```

Source CPU:

```bash
docker compose up -d
```

NVIDIA:

```bash
docker compose -f docker-compose.gpu.yml up -d
```

Then check:

```bash
docker ps --filter name=subgen
curl --fail http://127.0.0.1:9000/status
docker logs --tail 100 subgen
```

The first job downloads the model into `SUBGEN_MODEL_PATH`. Do not treat that initial delay as a hang unless the logs stop changing or show an error.

## 6. Optional Plex webhook

Folder scanning works without Plex credentials. For webhook-driven `library.new` or `media.play` events, set:

Plex webhooks require an active [Plex Pass for the server owner or administrator](https://support.plex.tv/articles/115002267687-webhooks/).

```dotenv
SUBGEN_BIND_ADDRESS=192.168.1.50
PLEX_SERVER=http://192.168.1.20:32400
PLEX_TOKEN=replace-with-your-token
```

Configure the Plex webhook URL as:

```text
http://192.168.1.50:9000/plex
```

Keep this on a trusted LAN. The Plex route is intentionally compatible with Plex's webhook request and does not use `SUBGEN_API_KEY`.

### Path mapping

Plex returns the path it knows for a media item. If that path is not the same path Subgen sees inside the container, configure a root replacement:

```dotenv
USE_PATH_MAPPING=True
PATH_MAPPING_FROM=/path/reported/by/plex
PATH_MAPPING_TO=/media
```

Check one real item before scanning an entire library.

## 7. Optional API key

The service binds to loopback by default. If a trusted remote tool needs `/asr`, `/batch`, `/detect-language`, or the OpenAI-compatible audio endpoints, generate a key:

```bash
openssl rand -hex 32
```

Store it only in `.env`:

```dotenv
SUBGEN_API_KEY=generated-value
```

Clients send it as `X-Subgen-Api-Key`. Do not publish the key in an issue or compose file.

## 8. Optional failure monitor

The monitor is safe by default: it reports but does not delete.

The host helpers require Python 3.10 or newer, Docker CLI/socket access, an existing service account, read/traverse access to the media tree, and write access to `SUBGEN_STATE_DIR`. Media write/delete access is required only when deletion is enabled.

```bash
cp monitor.env.example monitor.env
```

The supplied units assume:

- repository: `/opt/subgen`
- service user: `mediauser`
- service group: `media`

If those values do not match your host, edit `User` and `Group`, replace every `/opt/subgen` occurrence (including `WorkingDirectory`, `EnvironmentFile`, and `ExecStart`), and ensure the state directory is writable by the service user before copying the units.

```bash
sudo cp systemd/subgen-monitor.service /etc/systemd/system/
sudo cp systemd/subgen-repair.service systemd/subgen-repair.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now subgen-monitor.service subgen-repair.timer
```

Check:

```bash
systemctl status subgen-monitor.service
systemctl status subgen-repair.timer
cat /opt/subgen/monitor/subgen_failed_files.txt
```

Do not enable deletion until the report shows correct exact paths. See [cleanup configuration](./CONFIGURATION.md#optional-repeated-offender-deletion).

## Upgrade

Back up `.env`, `monitor.env`, and monitor state first.

```bash
git pull --ff-only
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
curl --fail http://127.0.0.1:9000/status
```

For predictable production deployments, use a release tag instead of an unreviewed branch and read [CHANGELOG.md](../CHANGELOG.md) before upgrading.

## Stop or uninstall

```bash
docker compose -f docker-compose.ghcr.yml down
sudo systemctl disable --now subgen-monitor.service subgen-repair.timer
```

This does not remove media, generated subtitles, models, or monitor state. Remove `./models`, `/opt/subgen/monitor`, and generated `.srt` files manually only if you intend to delete them.

## Troubleshooting checklist

1. `docker compose ... config --quiet` succeeds.
2. `PUID` and `PGID` can write beside the media file.
3. `TRANSCRIBE_FOLDERS` uses container paths, not host paths.
4. Plex webhook paths either match the container or have explicit path mapping.
5. The selected model supports translation; do not use Turbo or `.en` checkpoints.
6. Only one transcription is running.
7. The host has free RAM/VRAM above the configured container budget.
