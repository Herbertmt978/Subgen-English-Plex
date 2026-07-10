# Install Guide

This guide assumes a fairly normal setup:

- Docker already works on the host that will run Subgen
- your media is mounted somewhere sensible on that host
- you are happy editing a text file and copying a few files around

If that sounds about right, this should be straightforward.

One thing to keep in mind before you start:

- every path, username, and email shown in this repo is an example
- you are expected to swap those for the values that make sense on your own machine
- the Python scripts are the real logic, but the deployment files are example scaffolding

You now have two normal ways to install this repo:

- source install: use `docker-compose.yml` and keep the override mounted from the local repo
- package install: use `docker-compose.ghcr.yml` and pull the prebuilt image from GitHub Container Registry

If you are new and just want the least fiddly setup, the package install is usually the easier one.

Before first boot, assume you need to review these three files:

- `docker-compose.yml` or `docker-compose.ghcr.yml`
- `systemd/subgen-monitor.service`
- `monitor.env` if you create one

## 0. Decide whether you want source or package install

### Source install

Use this if you want the repo files on disk to be the thing Docker runs directly.

That means:

- `docker-compose.yml`
- local bind mount of `./subgen_override.py`

### Package install

Use this if you want Docker to pull a ready-made image from GitHub Packages instead.

That means:

- `docker-compose.ghcr.yml`
- image: `ghcr.io/herbertmt978/subgen-english-plex:latest`
- no local bind mount for `subgen_override.py`

The GHCR package for this repo is public, so people can pull it without signing in to GitHub first.

## 1. Put the repo somewhere sensible

Copy or clone the repo onto the machine that will run Subgen. In the examples below, I use:

```bash
/opt/subgen
```

If you prefer another location, that is fine. Just remember to keep the systemd service in sync with whatever location you choose.

## 2. Check the media paths first

Open the compose file you actually plan to use and look at the `volumes:` section.

The example uses:

```yaml
- /srv/media/PlexFilmsHD:/media/PlexFilmsHD
- /srv/media/PlexFilms:/media/PlexFilms
- /srv/media/PlexTVHD:/media/PlexTVHD
- /srv/media/PlexTV:/media/PlexTV
```

The left side is the real folder on your server.
The right side is the folder path Subgen sees inside the container.

If your media lives somewhere else, change the left side only. The `/media/...` part on the right is part of the internal layout used by this setup.

## 3. Check the user and group IDs

In the same compose file, look at:

```yaml
- PUID=1000
- PGID=1001
```

These tell the container which Linux user and group it should act as when reading and writing files.

If subtitles are not being written at all, this is one of the first places I would check.

To find the right values on your host:

```bash
id
```

## 4. Create the model folder

The compose file expects a model folder at:

```bash
/opt/subgen/models
```

Create it if needed:

```bash
mkdir -p /opt/subgen/models
```

Subgen will download the Whisper models there the first time it needs them.

## 5. Start Subgen

If you are doing a source install, from the repo folder run:

```bash
docker compose up -d
```

If you are doing a package install, run:

```bash
docker compose -f docker-compose.ghcr.yml up -d
```

Then check the basics:

```bash
docker ps
docker logs --tail 100 subgen
curl http://127.0.0.1:9000/status
```

If `/status` returns a small JSON response, the container is alive and listening.

## 6. Set up the monitor

The monitor does two useful jobs:

- it keeps a readable summary of failures
- when explicitly enabled, it deletes an exact regular file after the configured repeated-failure threshold

Copy the example config:

```bash
cp monitor.env.example monitor.env
```

You can leave it mostly alone to start with. If you do not want email alerts yet, that is perfectly fine.

## 7. Install the monitor as a service

Copy the systemd unit:

```bash
sudo cp systemd/subgen-monitor.service /etc/systemd/system/subgen-monitor.service
sudo cp systemd/subgen-repair.service systemd/subgen-repair.timer /etc/systemd/system/
```

Then reload systemd and enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now subgen-monitor.service
sudo systemctl enable --now subgen-repair.timer
```

Check that it is running:

```bash
systemctl status subgen-monitor.service
```

## 8. Know where to look later

When something goes wrong, these are the files and commands that usually help first:

- `docker logs subgen`
  What Subgen is doing right now
- `monitor/subgen_failed_files.txt`
  The short human-readable summary
- `monitor/subgen_failed_events.log`
  The longer event log

## 9. Optional: email alerts

Email is only used here for one specific case:

- the file looks like it ought to be English based on its audio metadata
- but Whisper detects another language

In practice that usually means one of three things:

- the metadata is wrong
- the content is mixed-language
- the file is odd enough that you may want to inspect it by hand

If you want those alerts, fill in the SMTP values in `monitor.env`.

If you leave them blank, nothing breaks. The monitor still records the events. It just does not send email.

## 10. How to tell it is doing useful work

Healthy logs usually look like some mix of:

- `Skipping ... Subtitles already exist in English.`
- `WORKER START ...`
- `Detected language: English`
- `WORKER FINISH ...`

That is what a healthy queue looks like: it skips the files that are already covered and works through the rest.

## 11. If the server feels too busy

The first settings worth changing are:

- `WHISPER_THREADS`
- `WHISPER_MODEL`
- `cpus`

Lower values make the server calmer, but they also make subtitle generation slower.

The defaults in this repo were chosen to be livable, not to win any speed contest.

If you are installing onto a separate NVIDIA machine instead of your Plex host, that is usually the cleaner answer. In that case, change:

- `TRANSCRIBE_DEVICE` to `cuda`
- `COMPUTE_TYPE` to `float16`
- add GPU access in Docker with `gpus: all`
