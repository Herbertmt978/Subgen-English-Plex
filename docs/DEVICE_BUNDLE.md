# Selected-device model bundle

## Set up Intel, AMD, or a mixture of GPUs

The ordinary CPU/NVIDIA installation is unchanged. Use this setup
when you want Vulkan acceleration or want to select more than one GPU.

Vulkan handles Intel and AMD graphics. NVIDIA uses CUDA. Each selected GPU
loads its own copy of the same Whisper model and takes separate chunks from
the file. Their memory is not added together: two 8 GiB cards do not become a
16 GiB card. Integrated graphics also need space in system RAM.

Start with one GPU. Confirm a file completes, then add another. A slower GPU
can delay the final join, so more devices do not always make a file finish sooner.
An explicit model choice never silently falls back to a smaller model.

### Linux Docker

Build the optional image on the machine where you normally build containers:

```bash
docker build --target vulkan -t subgen-english-plex:v0.5.0-vulkan .
```

This target includes the native worker, matching libraries, Mesa Vulkan
drivers and model-preparation tools. A plain build still produces the usual
CPU/CUDA image. You do not need the Vulkan SDK inside the running container.
The host needs a working graphics driver and a render node such as
`/dev/dri/renderD128`; passing a device does not install its kernel driver.

Create the model parent folder, then prepare the model you want. This example
uses `base` for a quick installation check. For library quality, choose a larger
model that your machine can accommodate, such as `large-v3`.

```bash
mkdir -p selected-models
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD/selected-models:/selected-models" \
  --entrypoint python3 subgen-english-plex:v0.5.0-vulkan \
  -m subgen_core.device_provisioning --model base \
  --output /selected-models/installed
```

Preparation downloads known OpenAI weights, verifies them, and converts them
on the CPU. It needs more RAM and disk space than inference because source
and converted weights coexist. Large-v3 paired preparation needs roughly
12 GiB of spare RAM after reserves, plus space for the source and intermediate
models. Use a larger machine for preparation if necessary; this is not a
requirement to leave that much RAM available during every subtitle job.
The command checks available RAM before starting the conversion and refuses
to overwrite an existing output directory.

Repeat `--model` to provide several choices for `WHISPER_MODEL=auto`. Subgen
then chooses from those prepared models, highest quality first. Add
`--with-cuda` if any selected worker will use NVIDIA. Both formats are then
converted from the same checkpoint, with every source tensor verified before
CUDA conversion. Don't combine unrelated downloads merely labelled “large”.

Check the visible GPUs before choosing an index:

```bash
docker run --rm --device /dev/dri/renderD128 \
  --entrypoint /opt/subgen-vulkan/subgen-vulkan-probe \
  subgen-english-plex:v0.5.0-vulkan
```

Set `SUBGEN_DEVICES=vulkan:0` and your model choice in `.env`. Run
`python3 configure_capacity.py` as described in the installation guide, then:

```bash
SUBGEN_VULKAN_IMAGE=subgen-english-plex:v0.5.0-vulkan \
  docker compose -f docker-compose.vulkan.yml up -d
```

The Vulkan Compose profile inherits the usual library, monitoring and memory
settings. It mounts the prepared models read-only. For another render node,
set `SUBGEN_RENDER_DEVICE`; to expose several nodes, add explicit `devices`
entries in your local Compose override. A CUDA/Vulkan Docker combination also
needs NVIDIA Container Toolkit access, as in the NVIDIA Compose profile.
Do not assume Docker Desktop exposes a Windows integrated GPU through Linux
`/dev/dri`. The Windows-native route below avoids that limitation.

### Windows native

Use 64-bit Python 3.12 or later and FFmpeg/FFprobe on PATH. Extract the matching
Windows x64 native package into a permanent folder, not a temporary download
directory. It contains Release builds of the worker and backend libraries.
You need the normal Microsoft Visual C++ x64 Redistributable and a working
Vulkan graphics driver, not Visual Studio or developer-only Debug DLLs.

Create a virtual environment in the source checkout. Install Torch first:

```powershell
python -m venv .venv
# Intel/AMD only:
.\.venv\Scripts\python.exe -m pip install torch==2.11.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cpu
# If NVIDIA will also be selected, use /whl/cu128 instead of /whl/cpu above.
.\.venv\Scripts\python.exe -m pip install -r requirements-native-windows.txt
.\.venv\Scripts\python.exe apply_stable_ts_fix.py
.\.venv\Scripts\python.exe apply_faster_whisper_fix.py
.\.venv\Scripts\python.exe -m pip check
```

The two correction scripts refuse unknown dependency versions. Do not ignore
a failure or install whatever happens to be newest to get past it.

For example, with the native files in `D:\Subgen\native`, prepare models in a
new directory whose parent already exists:

```powershell
.\.venv\Scripts\python.exe -m subgen_core.device_provisioning --model base --runtime D:\Subgen\native --output D:\Subgen\models
& D:\Subgen\native\subgen-vulkan-probe.exe
```

Use the reported Intel/AMD index, which may not be zero. Add `--with-cuda`
during preparation for a mixed configuration, then select e.g.
`cuda:0,vulkan:1`. Don't list the same physical NVIDIA card through both APIs.
The software rejects duplicate physical devices rather than loading it twice.

Set environment variables before starting the application:

```powershell
$env:SUBGEN_DEVICES='vulkan:1'  # Replace with the index actually reported above.
$env:SUBGEN_DEVICE_BUNDLE='D:\Subgen\models\bundle.json'
$env:SUBGEN_DEVICE_SCRATCH='D:\Subgen\scratch'  # Create this folder first.
$env:WHISPER_MODEL='base'       # Or a larger model you prepared.
$env:WHISPER_THREADS='2'
$env:TRANSCRIBE_FOLDERS='D:\Media\Movies'
$env:TRANSCRIBE_OR_TRANSLATE='translate'
$env:SHOULD_WHISPER_DETECT_AUDIO_LANGUAGE='True'
$env:SKIP_STARTUP_SCAN='False'
$env:MONITOR='True'
.\.venv\Scripts\python.exe -m uvicorn subgen_override:app --host 127.0.0.1 --port 9000
```

This binds the API to the local machine. Protect it with the usual API-key
and firewall settings before making it reachable elsewhere. Ctrl+C stops the
application and releases its workers. An Intel/AMD-only installation does not
need a CUDA card for language detection.

### Measure shared-RAM use on your machine

Calibration is optional. Without it, Subgen uses conservative estimates and
keeps checking available RAM during work. A measurement can avoid needlessly
rejecting a model that fits your particular integrated GPU.

Use representative spoken audio, not an empty or silent file. For five-minute
chunks the WAV must contain at least 310 seconds: the extra ten seconds cover
the overlap. It must be mono, 16 kHz, 16-bit PCM. Run calibration while the
machine has enough free memory for the initial conservative model check:

```bash
python -m subgen_core.native_calibration \
  --bundle /selected-models/installed/bundle.json \
  --output /selected-models/installed/calibrated.json \
  --device vulkan:0 --model base --audio /samples/speech.wav \
  --scratch /tmp --chunk-seconds 300 --task translate --threads 2
```

For Docker, run this inside the same Vulkan image with the GPU, model folder
and sample mounted; the model folder must be writable for this one setup step.
On Windows, use the virtual-environment Python and absolute Windows paths.
Point `SUBGEN_DEVICE_BUNDLE` at `calibrated.json` after the command succeeds.

The command completes three cold runs, checks the results, confirms each
worker has released, and saves measured peaks with a safety margin. System
process memory and Vulkan allocations can overlap; the reported shared-RAM
figure is deliberately an upper bound, not a precise physical-memory bill.
A failed or interrupted calibration does not publish a profile.

Profiles apply only to the model, native binaries, device, driver, operating
system, task and thread count measured. They also cap chunks at the tested
length. A changed driver or unmatched setting falls back to conservative
admission; it doesn't reuse somebody else's numbers. Translation and
original-language transcription need separate profiles. Calibration never
disables emergency memory checks or promises every recording will be correct.

## Bundle format reference

The candidate's optional GPU path reads `SUBGEN_DEVICE_BUNDLE` at startup.
This is a local provisioning record, not a download list or a claim that a
model has been calibrated on your hardware. The regular CPU/NVIDIA setup does
not need it when `SUBGEN_DEVICES` is blank.

The top-level JSON fields are:

| Field | Contents |
| --- | --- |
| `schema` | `subgen.device-bundle/v1` |
| `models` | A map of model names, each containing its `cuda` and/or `vulkan` variant. Up to sixteen model choices. |
| `cuda` | `runtime` (the CUDA runtime version) and `packages` (exact versions of `torch`, `ctranslate2`, `faster-whisper` and one installed stable-ts distribution). |
| `vulkan` | `probe` and `runtime_artifacts`, described below. |

Each model variant has `path` and `identity`. The path names the weight file:
`model.bin` for CUDA or the GGML file for Vulkan. Paths may be absolute or
relative to the bundle directory; parent-directory traversal is rejected.
The identity records `model`, `backend_format` (`ctranslate2` or `ggml`),
`precision`, `weights_sha256`, `size_bytes` and `source_checkpoint_sha256`.
Digests include the `sha256:` prefix.

CUDA variants also list `support_files`: the name, SHA-256 and size of every
loader-relevant tokenizer/configuration file. `config.json`, `tokenizer.json`
and `preprocessor_config.json` are required; a provisioned vocabulary file may
also be included. Each backend rechecks its files before loading the model.

The Vulkan `probe` has a `path` and an `identity` containing `component`,
`sha256` and `size_bytes`. `runtime_artifacts` is a list with the same shape
for the native worker and its libraries. The worker compares those identities
with the libraries actually loaded, rather than trusting filenames alone.

Matching model names are not enough. CUDA and Vulkan conversions must come
from the same verified source checkpoint, with matching float16 or float32
weight precision. Two files labelled “large” do not establish that. Hashes
bind a record to local bytes; they do not certify an unverified conversion or
make a locally generated bundle a signed release artifact.

Provisioning verifies the installed files. Do not copy
someone else's hashes into a bundle for different files, omit failed checks,
or point at a shared mutable download cache to get past validation. The
candidate currently reports missing provisioning as a setup failure and keeps
the media file unmarked. It does not switch GPUs or silently pick a smaller
explicitly requested model.

## An Intel or AMD GPU on its own

For Docker Desktop, keep `SUBGEN_DEVICE_SCRATCH` on the container's Linux
filesystem (for example `/tmp`) or a Linux Docker volume. Do not bind it to a
Windows drive. In the simulator test, a Windows-mounted scratch directory
allowed inference but failed when the subtitle journal truncated an anonymous
temporary file. That prevented the final subtitle from being written. Media
and model files can still use their separate Windows mounts. Scratch space is
temporary working storage, not the place to keep finished subtitles.

Selected CUDA workers require the pinned faster-whisper 1.2.1 language-window
correction (capability v2) in `apply_faster_whisper_fix.py`. The Docker build applies it; private
Python environments must apply it too. The helper verifies the exact source
hash and refuses unknown versions. A worker refuses an uncorrected runtime
before loading its model, including the earlier five-second candidate. CUDA now
checks up to ten seconds of leading audio; native Vulkan retains its separately
tested five-second check. Neither adds another model or subtitle-joining stage.
Vulkan needs the matching native v2 library and worker;
rebuild both and regenerate their hashes rather than reusing old bundle receipts.
Include all three pinned patches listed in `native/README.md`, including the
request-seed correction. The rebuilt worker also enforces positive-duration
closing timestamps during decoding. A language-v2 library alone does not prove
that these later corrections are present; bind the actual rebuilt artifacts.

A Vulkan-only bundle does not need a `cuda` section or CUDA model files. Select
the integrated GPU's discovered `vulkan:N` index; do not assume that it is always
zero on a machine with several adapters. The same native model detects language
and generates the transcript. There is no separate NVIDIA language detector.

Leave source language automatic to check it during decoding. A multilingual
model is needed for non-English speech and for `translate`, which produces
English subtitles. An `.en` model is English-only; choosing one cannot add
translation. Explicitly choosing the source language disables automatic switching.

This path requires the matching native package, including the patches described
in `native/README.md`. Intel/AMD shared GPU memory comes from system RAM; it is
not a second pool to add to the machine's available RAM. Standalone Linux/Intel
and Windows/AMD folder scans have passed; check the release notes for the
tested scope and limitations.
