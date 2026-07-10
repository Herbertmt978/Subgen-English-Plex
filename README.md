# Subgen English Translation Setup for Plex

This repository is a cleaned-up version of a Subgen setup that was built for a real Plex server and then written up properly afterwards.

The job it is meant to do is fairly narrow:

- create English subtitles for media in Plex libraries
- translate non-English speech into English subtitles
- keep scanning new media automatically
- remove files that repeatedly break the queue
- flag cases where the file metadata says "English audio" but Whisper hears something else

It is not trying to be a universal Subgen starter kit. It is a practical setup for people who want this specific behaviour and would rather start from something lived-in than something generic.

## Why this exists

The original motivation was simple:

- generate English subtitles locally for the media that is already on the server
- stop depending on Bazarr to hunt for subtitles from outside sources
- avoid turning subtitle generation into a bigger Radarr or Sonarr workflow problem

Bazarr is useful if your main goal is fetching subtitle files that already exist somewhere else.

This repo is for a different job:

- if subtitles do not already exist, make them
- if the audio is not English, translate it into English subtitles
- keep doing that for newly added media without needing a separate subtitle-fetching stack

If somebody already uses Radarr or Sonarr for downloads, that is fine. This setup just does not require them in order to keep subtitles flowing.

Everything in this repo that looks like a path, username, hostname, or email address is an example placeholder. Nothing here is meant to be pasted into a live server unchanged.

One practical consequence of that:

- the Python scripts in this repo are the real logic
- the compose file, service file, and monitor example config are safe public templates
- the repo can also publish a ready-made container image to GitHub Container Registry
- you should expect to edit those template files before first use

## What is actually in the repo

- `docker-compose.yml`
  The container definition.
- `docker-compose.ghcr.yml`
  The same basic setup, but pointed at the packaged image in GitHub Container Registry instead of a local bind-mounted override.
- `docker-compose.gpu.yml`
  A CUDA profile for a 24 GB NVIDIA GPU, with `large-v3`, one inference job, and a 20 GB container memory limit.
- `subgen_override.py`
  A custom Python override that replaces the stock `subgen.py` inside the container.
- `monitor_subgen_failures.py`
  A small monitor that follows the container logs and reacts to known failure cases.
- `repair_subgen_failures.py`
  A timer-friendly cleanup pass for exact files that repeatedly crash the worker.
- `monitor.env.example`
  Optional monitor settings, including SMTP if you want email alerts.
- `systemd/subgen-monitor.service`
  A service file so the monitor survives reboot.
- `docs/INSTALL.md`
  The install guide.
- `docs/CONFIGURATION.md`
  A plain-English explanation of the settings that matter.

## The important bit

This setup uses the standard `mccloud/subgen:latest` image, but it does not run the stock script inside that image unchanged.

This line is the key:

```yaml
- ./subgen_override.py:/subgen/subgen.py
```

So the real shape of the setup is:

- stock Subgen container
- custom runtime script
- custom monitor

That matters because some of the behaviour in this repo is not just "set these environment variables". Part of it lives in the Python override.

## Package support

Yes, this repo can be published as a package, but the sensible package format here is a container image, not an npm or NuGet package.

The repo now includes:

- a `Dockerfile` that bakes `subgen_override.py` into a custom image
- a GitHub Actions workflow at `.github/workflows/publish-ghcr.yml`
- a `docker-compose.ghcr.yml` example that uses the published image directly

The image name is:

```text
ghcr.io/herbertmt978/subgen-english-plex
```

The workflow publishes the image to GHCR, so people can either:

- pull the package directly with Docker
- or use `docker-compose.ghcr.yml` as their starting point instead of the source-based compose file

The package is now public, so people can pull it without signing in to GitHub first.

If you prefer the package route, the cleanest install path is usually:

```bash
docker pull ghcr.io/herbertmt978/subgen-english-plex:latest
```

or:

```bash
docker compose -f docker-compose.ghcr.yml up -d
```

## How it is meant to behave

In plain terms:

1. If a file already has English subtitles, leave it alone.
2. If it does not, try to make them.
3. If the spoken language is not English, translate the speech into English subtitles.
4. If a file keeps breaking Subgen, remove it so the queue can move on.
5. If metadata claims there is English audio but Whisper detects another language, log it and optionally send an email.

## What it is not trying to do

- It is not trying to preserve every bad file forever.
- It is not trying to make Intel Quick Sync run Whisper. Plex can use the Intel iGPU for video transcoding, but Whisper itself is better suited either to CPU or to a proper NVIDIA CUDA setup.
- It is not trying to hide every decision behind "smart defaults". A few settings are left visible on purpose because they are the ones people usually need to change.

## Resource profile

The included compose file is intentionally moderate and CPU-friendly:

- `cpus: 8.0`
- `WHISPER_MODEL=medium`
- `WHISPER_THREADS=8`
- `CONCURRENT_TRANSCRIPTIONS=1`
- `COMPUTE_TYPE=int8`

That is not the fastest possible setup. It is the kind of setup you can actually live with on a Plex machine that is doing other work.

If you are running on an NVIDIA box instead, the usual changes are:

- `TRANSCRIBE_DEVICE=cuda`
- `COMPUTE_TYPE=float16`
- `gpus: all`
- `WHISPER_MODEL=large-v3` when speech must be translated to English

That is closer to the kind of move you would make if you want Subgen off the Plex CPU and onto a separate GPU-backed VM.

For a 24 GB NVIDIA card, `docker-compose.gpu.yml` is the ready-made profile. It keeps concurrency at one, gives the larger model a 20 GB container memory limit, and keeps the model loaded for 15 minutes between bursts. Set `MEDIA_ROOT`, `SUBGEN_MODEL_PATH`, `SUBGEN_BIND_ADDRESS`, and a private `SUBGEN_API_KEY` in `.env` before starting it.

## Choosing a Whisper model

If you only want one short recommendation:

- `medium` is the sensible CPU default
- `large-v3` is the sensible NVIDIA default for this translation-first setup
- `large-v3-turbo` is fast for same-language transcription, but is not trained for translation

Very roughly, the model ladder looks like this:

- `small`
  Good for low-power CPU boxes, testing, or a server where subtitles are helpful but not mission-critical.
- `medium`
  The usual middle ground for CPU installs. Slower than `small`, but noticeably better on messy real media.
- `large-v3-turbo`
  Fast on NVIDIA for transcription, but a poor fit here: upstream Whisper explicitly warns that Turbo was not trained for translation and can return the original language.
- `large-v3`
  The recommended CUDA model when non-English speech must become English text. A 24 GB card has ample room at one concurrent job.
- `distil-large-v3`
  Fast on GPU, but mainly aimed at English speech recognition rather than this repo's multilingual-to-English translation workflow.

One important trap to avoid:

- do not pick the English-only `.en` models if your plan is to translate non-English audio into English subtitles

There is a longer plain-English model guide in [docs/CONFIGURATION.md](./docs/CONFIGURATION.md).

## First-time setup

If you are new to this repo, the easiest way to think about it is:

- `docker-compose.yml` runs Subgen itself
- `systemd/subgen-monitor.service` keeps the helper monitor alive after reboot
- `monitor.env` is optional monitor configuration, mainly deletion behaviour and SMTP

For a normal first install, those are the three files you should expect to look at.

The Python files are the real logic, but most people should not need to edit them just to get started.

If you are using the packaged image from GitHub Container Registry rather than running from source, the file you will usually edit is `docker-compose.ghcr.yml` instead of `docker-compose.yml`.

If you just want the least fiddly public install, start with `docker-compose.ghcr.yml`. It uses the already-published image and avoids the local bind-mounted override.

### 1. Decide where Subgen will run

Pick the machine that will actually do the subtitle work.

- If you are keeping it on a Plex box, the CPU-friendly defaults in this repo are a sensible starting point.
- If you have a separate NVIDIA machine, that is often the cleaner place to run it. In that case you will want `TRANSCRIBE_DEVICE=cuda`, `COMPUTE_TYPE=float16`, and `gpus: all`.

For the rest of the examples below, I will assume the repo lives at:

```bash
/opt/subgen
```

### 2. Put the repo on that machine

Copy or clone the repo onto the host that will run Subgen.

```bash
cd /opt
git clone <your-repo-url> subgen
cd /opt/subgen
```

If you already put it somewhere else, that is fine. Just remember to keep the systemd service paths in sync with the folder you chose.

### 3. Edit `docker-compose.yml`

This is the file that matters most. It controls:

- which media folders Subgen can see
- which user it runs as
- whether it uses CPU or NVIDIA CUDA
- which Whisper model and limits it uses

If you are using the packaged image instead of building from source, do the same edits in `docker-compose.ghcr.yml`.

The first part most people need to change is the `volumes:` section:

```yaml
- /srv/media/PlexFilmsHD:/media/PlexFilmsHD
- /srv/media/PlexFilms:/media/PlexFilms
- /srv/media/PlexTVHD:/media/PlexTVHD
- /srv/media/PlexTV:/media/PlexTV
```

The left side is your real host path.
The right side is the path Subgen expects inside the container.

If your media folders live somewhere else, change the left side only.

### 4. Check `PUID` and `PGID`

These tell the container which Linux user and group it should act as when it reads media files and writes subtitles.

Find the right values on the host:

```bash
id
```

Then update the matching lines in `docker-compose.yml`:

```yaml
- PUID=1000
- PGID=1001
```

If these are wrong, the usual symptom is simple: Subgen starts, but subtitles do not get written properly.

### 5. Choose CPU mode or NVIDIA mode

If you are staying on a normal Linux host, the included defaults are already set up to be moderate and CPU-friendly.

If you are moving Subgen onto an NVIDIA machine, these are the main things to change in `docker-compose.yml`:

```yaml
gpus: all
environment:
  - TRANSCRIBE_DEVICE=cuda
  - COMPUTE_TYPE=float16
  - WHISPER_MODEL=large-v3
```

If the machine does not actually have an NVIDIA GPU available to Docker, do not turn this on.

If you skip the GPU part on an NVIDIA host, the container will still run, but it will not actually use CUDA the way you expect.

### 6. Create the folders Subgen expects

At minimum, create the model folder before first boot:

```bash
mkdir -p /opt/subgen/models
```

If you also plan to use the helper monitor, it is tidy to create the monitor folder too:

```bash
mkdir -p /opt/subgen/monitor
```

### 7. Start Subgen

From the repo folder, if you are running from source:

```bash
cd /opt/subgen
docker compose up -d
```

If you are using the packaged image instead:

```bash
cd /opt/subgen
docker compose -f docker-compose.ghcr.yml up -d
```

Both approaches start the container in the background. The difference is just where the custom Subgen logic comes from:

- source install: from your local repo files
- package install: from the prebuilt GHCR image

### 8. Check that the container is alive

Run these three checks:

```bash
docker ps
docker logs --tail 100 subgen
curl http://127.0.0.1:9000/status
```

What you want to see:

- the `subgen` container is running
- the logs show either skipped files or active work
- `/status` returns a small JSON response

If that all looks right, the core install is working.

### 9. Optional: create `monitor.env`

The helper monitor watches logs and records failures. With deletion enabled, it waits for three failures by default and only removes an exact, regular file that is still inside the configured media root. It never recursively deletes a directory and never creates an empty subtitle as a skip marker.

If you want to use it, start by copying the example:

```bash
cp monitor.env.example monitor.env
```

You can leave the SMTP lines blank if you do not want email alerts yet.
That does not break anything. It just means the monitor will log those events without trying to send mail.

### 10. Install the systemd monitor service

Copy the unit file into place:

```bash
sudo cp systemd/subgen-monitor.service /etc/systemd/system/subgen-monitor.service
```

Then reload systemd and enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now subgen-monitor.service
```

Check it:

```bash
systemctl status subgen-monitor.service
```

If it shows `active (running)`, the helper monitor is now surviving reboots on its own.

### 11. If you want the longer walkthrough

If you want the same process with more explanation, use [docs/INSTALL.md](./docs/INSTALL.md).

If you want a plain-English explanation of the settings in the compose file, use [docs/CONFIGURATION.md](./docs/CONFIGURATION.md).

## Why the docs sound like this

Most people using something like this do not want a pile of container boilerplate. They want to know:

- what does this setting do
- what should I change
- what should I leave alone
- what will make the server noisier
- how do I know it is working

That is the angle the docs take.

## A few sanity checks

After setup, the first checks I would run are:

```bash
docker ps
curl http://127.0.0.1:9000/status
docker logs --tail 100 subgen
systemctl status subgen-monitor.service
```

What you want to see:

- the `subgen` container is running
- `/status` returns a small JSON response
- the logs show either skipped files or active work
- the monitor service is `active (running)`

## If you want to tune it

Start with [docs/CONFIGURATION.md](./docs/CONFIGURATION.md).

That file explains the settings people usually care about:

- CPU limits
- Whisper model size
- thread count
- translation mode
- duplicate-subtitle protection
- deletion behaviour in the monitor

## Final note

There are easier ways to throw a working setup onto GitHub. Most of them become annoying to trust a few months later because nobody remembers which parts were deliberate and which parts were copied from somewhere else.

The aim here was to leave behind something that still makes sense when somebody opens it later and asks: what does this actually do, and which parts are safe to change?
