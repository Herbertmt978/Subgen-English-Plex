"""Provisioning does not trust filenames, overwrite models or launch GPUs."""
import hashlib
import io
import json
from pathlib import Path
import subprocess

import pytest

from subgen_core import device_provisioning as p


def test_verified_download(tmp_path, monkeypatch):
    monkeypatch.setattr(p.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(b'model'))
    path = p.download_verified('https://example.test/model', tmp_path/'model',
                              hashlib.sha256(b'model').hexdigest(), maximum_bytes=8)
    assert path.read_bytes() == b'model'
    with pytest.raises(FileExistsError):
        p.download_verified('unused', path, '0'*64, maximum_bytes=8)
    assert path.read_bytes() == b'model'


@pytest.mark.parametrize('limit,digest', [(2,hashlib.sha256(b'model').hexdigest()), (8,'0'*64)])
def test_failed_download_never_publishes(tmp_path, monkeypatch, limit, digest):
    monkeypatch.setattr(p.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(b'model'))
    with pytest.raises(ValueError):
        p.download_verified('https://example.test/model', tmp_path/'model', digest, maximum_bytes=limit)
    assert not (tmp_path/'model').exists()
    assert (tmp_path/'model.part').exists()


@pytest.mark.parametrize('models', [[], ['base','base'], ['../medium'], ['large']])
def test_invalid_model_before_any_mutation(tmp_path, models):
    with pytest.raises(ValueError):
        p.prepare_native(models, tmp_path/'new', tmp_path)
    assert not (tmp_path/'new').exists()


def test_existing_output_preserved(tmp_path):
    (tmp_path/'keep').write_text('existing model')
    with pytest.raises(ValueError, match='existing models'):
        p.prepare_native(['base'], tmp_path, tmp_path)
    assert (tmp_path/'keep').read_text() == 'existing model'


def fake_setup(tmp_path, monkeypatch):
    checkpoint = tmp_path/'base.pt'
    checkpoint.write_bytes(b'verified checkpoint')
    monkeypatch.setitem(p.CHECKPOINTS, 'base', p.file_digest(checkpoint))
    monkeypatch.setattr(p, 'CONVERSION_ASSETS', {})
    monkeypatch.setattr(p, 'native_manifest', lambda _: {'probe':{}, 'runtime_artifacts':[]})
    monkeypatch.setattr(p, 'load_device_bundle', lambda path: json.loads(path.read_text()))
    calls = []
    def convert(command, **options):
        calls.append((command,options))
        (Path(command[-1])/'ggml-model.bin').write_bytes(b'converted weights')
    monkeypatch.setattr(p.subprocess,'run',convert)
    return checkpoint, calls


def test_conversion_is_cpu_only_and_binds_checkpoint(tmp_path, monkeypatch):
    checkpoint,calls = fake_setup(tmp_path,monkeypatch)
    path = p.prepare_native(['base'],tmp_path/'prepared',tmp_path,checkpoints=tmp_path)
    identity = json.loads(path.read_text())['models']['base']['vulkan']['identity']
    assert identity['source_checkpoint_sha256'] == 'sha256:'+p.file_digest(checkpoint)
    assert calls[0][1]['env']['CUDA_VISIBLE_DEVICES'] == ''
    assert calls[0][1]['timeout'] == 3600
    assert not (path.parent/'bundle.pending.json').exists()


def test_wrong_checkpoint_never_runs_converter(tmp_path, monkeypatch):
    checkpoint,calls = fake_setup(tmp_path,monkeypatch)
    checkpoint.write_bytes(b'wrong checkpoint')
    with pytest.raises(ValueError, match='does not match'):
        p.prepare_native(['base'],tmp_path/'prepared',tmp_path,checkpoints=tmp_path)
    assert not calls
    assert not (tmp_path/'prepared'/'bundle.json').exists()


def test_conversion_failure_never_publishes_bundle(tmp_path, monkeypatch):
    fake_setup(tmp_path,monkeypatch)
    def fail(*args,**kwargs):
        raise subprocess.CalledProcessError(1,args[0])
    monkeypatch.setattr(p.subprocess,'run',fail)
    with pytest.raises(subprocess.CalledProcessError):
        p.prepare_native(['base'],tmp_path/'prepared',tmp_path,checkpoints=tmp_path)
    assert not (tmp_path/'prepared'/'bundle.json').exists()


def test_optional_docker_target_does_not_change_default_image():
    dockerfile = (Path(__file__).resolve().parents[1]/'Dockerfile').read_text()
    assert dockerfile.rstrip().endswith('FROM runtime AS default')
    assert 'FROM runtime AS vulkan\n' in dockerfile
    assert p.WHISPER_CPP_REVISION in dockerfile
    assert 'sha256sum -c -' in dockerfile
    for name in ('vulkan-budget', 'language-segments', 'request-seed'):
        assert 'git apply --check /build/native/patches/whisper-cpp-'+name+'.patch' in dockerfile


def test_paired_conversion_uses_same_source_and_publishes_both(tmp_path,monkeypatch):
    from subgen_core import checkpoint_conversion
    checkpoint,calls=fake_setup(tmp_path,monkeypatch)
    monkeypatch.setattr(p,'cuda_manifest',lambda:{'runtime':'12.8','packages':{}})
    monkeypatch.setattr(checkpoint_conversion,'prepare_conversion_assets',lambda *a:None)
    native_convert=p.subprocess.run
    def convert(command,**options):
        if '--model' not in command:return native_convert(command,**options)
        assert options['env']['CUDA_VISIBLE_DEVICES']==''
        assert command[command.index('--checkpoint')+1]==str(checkpoint)
        target=Path(command[command.index('--target')+1])/'ct2'
        target.mkdir()
        for name in ('model.bin','config.json','tokenizer.json','preprocessor_config.json'):
            (target/name).write_bytes(name.encode())
    monkeypatch.setattr(p.subprocess,'run',convert)
    output=p.prepare_native(['base'],tmp_path/'prepared',tmp_path,checkpoints=tmp_path,with_cuda=True)
    data=json.loads(output.read_text())
    variants=data['models']['base']
    assert set(variants)=={'cuda','vulkan'}
    assert variants['cuda']['identity']['source_checkpoint_sha256']==variants['vulkan']['identity']['source_checkpoint_sha256']
    assert len(variants['cuda']['support_files'])==3


def test_conversion_memory_is_checked_before_large_tensor_allocation(tmp_path,monkeypatch):
    from subgen_core import resource_probes
    checkpoint=tmp_path/'base.pt';checkpoint.write_bytes(b'checkpoint')
    monkeypatch.setattr(resource_probes,'read_pressure_sample',lambda:resource_probes.PressureSample(
        host_total_bytes=4*1024**3,host_available_bytes=1024**3))
    with pytest.raises(ValueError,match='Prepare this model on a larger machine'):
        p.check_conversion_capacity(checkpoint,with_cuda=True)
