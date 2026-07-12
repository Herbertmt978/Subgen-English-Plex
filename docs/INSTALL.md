# Installation guide

The [README quick start](../README.md#quick-start) is enough for a normal installation. This page covers the decisions and checks that are useful when deploying on a real media server.

## 1. Choose a deployment

| Deployment | Command | Use it when |
| --- | --- | --- |
| Packaged CPU | `docker compose -f docker-compose.ghcr.yml up -d` | Recommended first installation. The published GHCR image contains the facade, helper, and `subgen_core` package. |
| Source CPU | `docker compose up -d` | You want the checked-out facade, helper, and `subgen_core` package mounted read-only. |
| Packaged NVIDIA | `docker compose -f docker-compose.gpu.yml up -d` | The same packaged runtime with NVIDIA GPU access through NVIDIA Container Toolkit. |

The public default is CPU `medium`, `int8`, four threads, one transcription, and a 10 GB memory limit. Read the [hardware guide](../README.md#model-and-hardware-guide) before changing the model.

The packaged profiles need no source-code mounts and default to the release-tagged `v0.3.0` image. Set `SUBGEN_IMAGE` only when deliberately testing another tag or immutable digest. The source profile mounts every Python component explicitly, so updating a checkout updates the complete modular runtime together.

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

`MEDIA_ROOT` is a host path. It is mounted at `/media` inside the container, so `TRANSCRIBE_FOLDERS` must use paths beneath `/media`. The value may be one media file, one folder, or a pipe-separated mixture of files and folders; Plex and *Arr services are not required.

For example, a one-file startup run can use:

```dotenv
TRANSCRIBE_FOLDERS=/media/Movies/Example.mkv
```

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

Plex and Jellyfin server/token settings are blank by default. Leaving them blank disables those integrations; standalone file and folder processing needs no media-server settings.

Plex webhooks require an active [Plex Pass for the server owner or administrator](https://support.plex.tv/articles/115002267687-webhooks/).

For webhook-driven `library.new` or `media.play` events, set your own values, for example:

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

The host helpers require Python 3.10+, Docker CLI/socket access, an existing service account, media traverse/read permissions, and write access to `SUBGEN_STATE_DIR`. Deletion additionally requires permission to remove the exact media entry.

```bash
cp monitor.env.example monitor.env
sudo install -d -m 700 -o mediauser -g media /opt/subgen/monitor
```

The supplied units assume:

- repository: `/opt/subgen`
- service user: `mediauser`
- service group: `media`

If those values do not match your host, edit `User` and `Group` and replace every `/opt/subgen` occurrence in `WorkingDirectory`, `EnvironmentFile`, and `ExecStart` before copying the units.

The state directory must be local, owned by the service user, and not group/world writable. The monitor refuses a symlink state directory. Keep the repository together: both operational scripts import `subgen_ops_safety.py` from `/opt/subgen`.

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

When upgrading existing monitor state, start the monitor once and confirm it has reset legacy path-only counts and written file-generation fingerprints before setting `SUBGEN_REPAIR_ACTION=delete`. A pending delete is paused whenever either current kill switch is off.

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
3. Every file or folder in `TRANSCRIBE_FOLDERS` uses a container path, not a host path.
4. Plex webhook paths either match the container or have explicit path mapping.
5. The selected model supports translation; do not use Turbo or `.en` checkpoints.
6. Only one transcription is running.
7. The host has free RAM/VRAM above the configured container budget.
