"""Read-only provisioning manifest for selected-device workers.

The bundle binds existing model/native identity types to local files. It never
downloads or converts weights, executes manifest-supplied commands, or creates
memory envelopes. Conversion provenance must be established when provisioning.
"""
from dataclasses import dataclass
import json
from pathlib import Path

from .model_envelope_catalog import (ModelArtifactIdentity, ModelSupportFileIdentity,
    NativeArtifactIdentity, validate_cohort_model_identity)
from .resident_worker import _unique_object
from .native_memory_profile import NativeMemoryProfile


def _shape(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError('Device bundle has missing or unknown fields')
    return value


def _path(root, value):
    if not isinstance(value, str) or not value or len(value) > 4096 or '\x00' in value:
        raise ValueError('Device bundle requires local artifact paths')
    path = Path(value)
    if '..' in path.parts:
        raise ValueError('Device bundle paths must not traverse parent directories')
    path = path if path.is_absolute() else root / path
    if path.is_symlink() or not path.exists():
        raise ValueError('A provisioned device artifact is missing or is a symbolic link')
    return path


@dataclass(frozen=True)
class ProvisionedModel:
    path: Path
    identity: ModelArtifactIdentity
    support_files: tuple = ()


@dataclass(frozen=True)
class DeviceBundle:
    models: dict
    native_artifacts: dict
    vulkan_probe: tuple | None
    cuda_runtime: str | None
    cuda_packages: dict
    native_profiles: tuple = ()


def load_device_bundle(filename):
    path = Path(filename)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('SUBGEN_DEVICE_BUNDLE must name an absolute, regular local file')
    with path.open('rb') as stream:
        raw = stream.read(262145)
    if len(raw) > 262144:
        raise ValueError('Device bundle exceeds 256 KiB')
    data = _shape(json.loads(raw, object_pairs_hook=_unique_object),
        {'schema', 'models'}, {'vulkan', 'cuda', 'native_profiles'})
    if data['schema'] != 'subgen.device-bundle/v1':
        raise ValueError('Unsupported device bundle version')
    if not isinstance(data['models'], dict) or not 1 <= len(data['models']) <= 16:
        raise ValueError('Device bundle needs between one and sixteen model choices')
    models = {}
    for name, variants in data['models'].items():
        _shape(variants, (), {'cuda', 'vulkan'})
        if not variants:
            raise ValueError('Provisioned model has no backend')
        entries = {}
        for backend, item in variants.items():
            _shape(item, {'path', 'identity'}, {'support_files'} if backend == 'cuda' else ())
            identity = ModelArtifactIdentity(**item['identity'])
            if identity.model != name or identity.backend_format != ('ctranslate2' if backend == 'cuda' else 'ggml'):
                raise ValueError('Device model name or backend contradicts its artifact')
            support = item.get('support_files', [])
            if not isinstance(support, list) or len(support) > 5:
                raise ValueError('Invalid model supporting file list')
            entries[backend] = ProvisionedModel(_path(path.parent, item['path']), identity,
                tuple(ModelSupportFileIdentity(**value) for value in support))
        validate_cohort_model_identity(tuple(entry.identity for entry in entries.values()))
        models[name] = entries
    native, probe, packages, cuda_runtime = {}, None, {}, None
    if 'vulkan' in data:
        config = _shape(data['vulkan'], {'probe', 'runtime_artifacts'})
        item = _shape(config['probe'], {'path', 'identity'})
        probe = (_path(path.parent, item['path']), NativeArtifactIdentity(**item['identity']))
        items = config['runtime_artifacts']
        if not isinstance(items, list) or not 4 <= len(items) <= 32:
            raise ValueError('Device bundle needs its complete native runtime manifest')
        for item in items:
            _shape(item, {'path', 'identity'})
            artifact_path = _path(path.parent, item['path'])
            if str(artifact_path) in native:
                raise ValueError('Device bundle repeats a native artifact path')
            native[str(artifact_path)] = NativeArtifactIdentity(**item['identity'])
    if 'cuda' in data:
        config = _shape(data['cuda'], {'runtime', 'packages'})
        packages, cuda_runtime = config['packages'], config['runtime']
        required = {'faster-whisper', 'ctranslate2', 'torch'}
        if (not isinstance(packages, dict) or not required <= packages.keys()
                or packages.keys() - required not in ({'stable-ts'}, {'stable-ts-whisperless'})
                or any(not isinstance(v, str) or not v or len(v) > 128 for v in packages.values())
                or not isinstance(cuda_runtime, str) or not cuda_runtime or len(cuda_runtime) > 64):
            raise ValueError('Device bundle needs exact CUDA package/runtime versions')
    profiles = data.get('native_profiles', [])
    if not isinstance(profiles, list) or len(profiles) > 32:
        raise ValueError('Device bundle supports at most thirty-two native profiles')
    profiles = tuple(NativeMemoryProfile.from_dict(item) for item in profiles)
    if len({p.key for p in profiles}) != len(profiles):
        raise ValueError('Device bundle repeats a native profile identity')
    return DeviceBundle(models, native, probe, cuda_runtime, packages, profiles)
