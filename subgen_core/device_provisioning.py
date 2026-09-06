"""Owner-operated native model provisioning, separate from runtime inference.

Convert a hash-verified OpenAI checkpoint with the pinned upstream converter.
No model download or conversion happens implicitly during a library scan.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import urllib.request

from .device_bundle import load_device_bundle
from .model_envelope_catalog import ModelArtifactIdentity, ModelSupportFileIdentity, NativeArtifactIdentity

WHISPER_CPP_REVISION = '52a939a2a762224e255d366c1182b2af4dd1a032'
OPENAI_REVISION = '86098128c0b4f24f0e2aa2994de830614b474227'
# OpenAI's immutable checkpoint URLs include these digests. Never accept an
# arbitrary pickle checkpoint just because a user-supplied receipt names it.
CHECKPOINTS = {
    'tiny.en': 'd3dd57d32accea0b295c96e26691aa14d8822fac7d9d27d5dc00b4ca2826dd03',
    'tiny': '65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9',
    'base.en': '25a8566e1d0c1e2231d1c762132cd20e0f96a85d16145c3a00adf5d1ac670ead',
    'base': 'ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e',
    'small.en': 'f953ad0fd29cacd07d5a9eda5624af0f6bcf2258be67c92b79389873d91e0872',
    'small': '9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794',
    'medium.en': 'd7440d1dc186f76616474e0ff0b3b6b879abc9d1a4926b7adfa41db2d497ab4f',
    'medium': '345ae4da62f9b3d59415adc60127b97c714f32e89e936602e85993674d08dcb1',
    'large-v1': 'e4b87e7e0bf463eb8e6956e646f1e277e901512310def2c24bf0e11bd3c28e9a',
    'large-v2': '81f7c96c852ee8fc832187b0132e569d6c3065a3252ed18e56effd0b6a73e524',
    'large-v3': 'e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb',
}
CONVERSION_ASSETS = {
    'convert-pt-to-ggml.py': (
        f'https://raw.githubusercontent.com/ggml-org/whisper.cpp/{WHISPER_CPP_REVISION}/models/convert-pt-to-ggml.py',
        'e874333f95c52725c23541b39e71594e01442a2a687c96e2e882493c45b887a2'),
    'whisper/assets/mel_filters.npz': (
        f'https://raw.githubusercontent.com/openai/whisper/{OPENAI_REVISION}/whisper/assets/mel_filters.npz',
        '7450ae70723a5ef9d341e3cee628c7cb0177f36ce42c44b7ed2bf3325f0f6d4c'),
    'whisper/assets/gpt2.tiktoken': (
        f'https://raw.githubusercontent.com/openai/whisper/{OPENAI_REVISION}/whisper/assets/gpt2.tiktoken',
        '306cd27f03c1a714eca7108e03d66b7dc042abe8c258b44c199a7ed9838dd930'),
    'whisper/assets/multilingual.tiktoken': (
        f'https://raw.githubusercontent.com/openai/whisper/{OPENAI_REVISION}/whisper/assets/multilingual.tiktoken',
        'b34b360dbb493e781e479794586d661700670d65564001f23024971d1f2fa126'),
}


def file_digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Provisioning requires a regular, non-symlink file')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def download_verified(url, destination, expected_digest, *, maximum_bytes):
    """Download into a fresh owned path; failed bytes never become an artifact."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + '.part')
    if destination.exists() or destination.is_symlink() or part.exists() or part.is_symlink():
        raise FileExistsError('Provisioning never overwrites an existing artifact')
    with part.open('xb') as output:
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                size = 0
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > maximum_bytes:
                        raise ValueError('Download exceeded its artifact size limit')
                    output.write(block)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            # Keep the incomplete .part for diagnosis; it is never used.
            raise
    if file_digest(part) != expected_digest:
        raise ValueError('Downloaded artifact failed SHA-256 verification')
    part.rename(destination)
    return destination


def native_manifest(runtime_directory, *, loader=None):
    """Bind real files, including canonical targets of the linker's aliases."""
    root = Path(runtime_directory)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError('Native runtime must be an absolute installed directory')
    windows = os.name == 'nt'
    extension = '.exe' if windows else ''
    files = {'worker': root / ('subgen-whisper-worker' + extension)}
    for component in ('whisper', 'ggml', 'ggml-base', 'ggml-cpu', 'ggml-vulkan'):
        filename = component + '.dll' if windows else 'lib' + component + '.so'
        candidates = [p.resolve(strict=True) for p in (root/filename, root/'runtime'/filename) if p.exists()]
        if len(set(candidates)) != 1:
            raise ValueError('Missing or ambiguous native component: ' + component)
        files[component] = candidates[0]
    if loader is None:
        loader = (Path(os.environ['SystemRoot'])/'System32'/'vulkan-1.dll' if windows else
                  Path('/usr/lib')/(platform.machine()+'-linux-gnu')/'libvulkan.so.1')
    files['vulkan-loader'] = Path(loader).resolve(strict=True)
    def entry(component, path):
        identity = NativeArtifactIdentity(component, 'sha256:'+file_digest(path), path.stat().st_size)
        return {'path': str(path), 'identity': asdict(identity)}
    return {'probe': entry('vulkan-probe', root/('subgen-vulkan-probe'+extension)),
            'runtime_artifacts': [entry(component, path) for component, path in files.items()]}


def cuda_manifest():
    import torch
    from .cuda_worker_entry import runtime_versions
    if not torch.version.cuda:
        raise ValueError('Mixed CUDA preparation needs a CUDA-enabled runtime, not CPU-only Torch')
    return {'runtime':torch.version.cuda,'packages':runtime_versions()}


def cuda_model_entry(model, directory):
    directory = Path(directory)
    weights = directory/'model.bin'
    identity = ModelArtifactIdentity(model,'ctranslate2','float16','sha256:'+file_digest(weights),
                                     weights.stat().st_size,'sha256:'+CHECKPOINTS[model])
    support = []
    for path in sorted(directory.iterdir()):
        if path.name != 'model.bin':
            support.append(asdict(ModelSupportFileIdentity(path.name,'sha256:'+file_digest(path),path.stat().st_size)))
    return {'path':str(weights),'identity':asdict(identity),'support_files':support}


def check_conversion_capacity(checkpoint, *, with_cuda):
    """Preparation holds source/converted tensors, unlike chunked inference."""
    from .resource_probes import read_pressure_sample
    from .resource_management import host_reserve_bytes
    sample = read_pressure_sample()
    if sample.host_available_bytes is None or sample.host_total_bytes is None:
        raise ValueError('Cannot read RAM availability for model conversion')
    available = sample.host_available_bytes-host_reserve_bytes(sample.host_total_bytes)
    if sample.cgroup_limit_bytes is not None:
        if sample.cgroup_current_bytes is None:
            raise ValueError('Cannot read container RAM availability for model conversion')
        available = min(available,sample.cgroup_limit_bytes-sample.cgroup_current_bytes-512*1024**2)
    required = max(2*1024**3,Path(checkpoint).stat().st_size*(4 if with_cuda else 3))
    if available < required:
        raise ValueError(f'Model preparation needs about {required/1024**3:.1f} GiB of spare RAM; '
                         f'{max(0,available)/1024**3:.1f} GiB available after reserves. '
                         'Prepare this model on a larger machine; inference needs less memory.')


def prepare_native(models, output, runtime_directory, *, checkpoints=None, with_cuda=False):
    """Prepare one or more model choices without loading a GPU model."""
    if not models or len(set(models)) != len(models) or any(m not in CHECKPOINTS for m in models):
        raise ValueError('Select distinct supported Whisper model names')
    output = Path(output)
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise ValueError('Choose a new absolute output directory; existing models are preserved')
    native = native_manifest(runtime_directory)
    cuda = cuda_manifest() if with_cuda else None
    output.mkdir(mode=0o700, parents=False)
    assets = output/'conversion'
    for name, (url, digest) in CONVERSION_ASSETS.items():
        download_verified(url, assets/name, digest, maximum_bytes=16*1024**2)
    bundle = {'schema':'subgen.device-bundle/v1', 'models':{}, 'vulkan':native}
    if cuda:
        bundle['cuda'] = cuda
    for model in models:
        expected = CHECKPOINTS[model]
        if checkpoints:
            checkpoint = Path(checkpoints)/(model+'.pt')
            if file_digest(checkpoint) != expected:
                raise ValueError('Checkpoint does not match the selected OpenAI model')
        else:
            checkpoint = download_verified(
                f'https://openaipublic.azureedge.net/main/whisper/models/{expected}/{model}.pt',
                output/'checkpoints'/(model+'.pt'), expected, maximum_bytes=4*1024**3)
        target = output/model
        target.mkdir()
        check_conversion_capacity(checkpoint,with_cuda=with_cuda)
        print('Preparing '+model+' for Intel/AMD: CPU conversion, no GPU inference', flush=True)
        environment = dict(os.environ, CUDA_VISIBLE_DEVICES='', OMP_NUM_THREADS='2', MKL_NUM_THREADS='2')
        with (target/'conversion.log').open('x', encoding='utf8') as log:
            subprocess.run([sys.executable, str(assets/'convert-pt-to-ggml.py'), str(checkpoint),
                            str(assets), str(target)], env=environment, stdout=log,
                           stderr=subprocess.STDOUT, check=True, timeout=3600)
        if file_digest(checkpoint) != expected:
            raise ValueError('Source checkpoint changed during conversion')
        weights = target/'ggml-model.bin'
        identity = ModelArtifactIdentity(model, 'ggml', 'float16', 'sha256:'+file_digest(weights),
                                         weights.stat().st_size, 'sha256:'+expected)
        bundle['models'][model] = {'vulkan': {'path':str(weights), 'identity':asdict(identity)}}
        if with_cuda:
            from .checkpoint_conversion import prepare_conversion_assets
            prepare_conversion_assets(target,model)
            print('Preparing '+model+' for NVIDIA from the same checkpoint; verifying every source tensor',flush=True)
            with (target/'cuda-conversion.log').open('x',encoding='utf8') as log:
                subprocess.run([sys.executable,'-m','subgen_core.checkpoint_conversion',
                    '--checkpoint',str(checkpoint),'--assets',str(assets),'--target',str(target),'--model',model],
                    env=environment,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=3600)
            if file_digest(checkpoint) != expected:
                raise ValueError('Source checkpoint changed during CUDA conversion')
            bundle['models'][model]['cuda'] = cuda_model_entry(model,target/'ct2')
    pending = output/'bundle.pending.json'
    with pending.open('x', encoding='utf8') as stream:
        json.dump(bundle, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    load_device_bundle(pending)
    pending.rename(output/'bundle.json')
    print('Models prepared. SUBGEN_DEVICE_BUNDLE='+str(output/'bundle.json'), flush=True)
    return output/'bundle.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', action='append', required=True, choices=sorted(CHECKPOINTS))
    parser.add_argument('--output', required=True, help='New absolute model directory')
    parser.add_argument('--runtime', default='/opt/subgen-vulkan', help='Installed native runtime directory')
    parser.add_argument('--checkpoints', help='Optional directory of existing verified OpenAI .pt checkpoints')
    parser.add_argument('--with-cuda',action='store_true',help='Also convert the same checkpoints for NVIDIA workers')
    args = parser.parse_args()
    prepare_native(args.model, args.output, args.runtime, checkpoints=args.checkpoints,with_cuda=args.with_cuda)


if __name__ == '__main__':
    main()
