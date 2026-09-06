from dataclasses import replace
import json
import os
import time

import pytest

from subgen_core.native_memory_profile import NativeMemoryProfile, NativeRunPeak, native_profile_key, MIB
from subgen_core.model_envelope_catalog import ModelArtifactIdentity, NativeArtifactIdentity
from subgen_core.vulkan_probe import VulkanDeviceObservation
from subgen_core.resource_management import native_model_load_requirement, model_load_requirement
from subgen_core.resource_probes import read_process_peak_bytes


def observation(**updates):
    fields=dict(physical_index=0,name='Test integrated GPU',uuid='1'*32,pci_id=None,vendor_id=4098,
        device_id=1,driver_version_raw=2,api_version_raw=3,memory_topology='shared',budget_supported=True,
        heaps=(),observed_at=time.monotonic())
    fields.update(updates)
    return VulkanDeviceObservation(**fields)


def artifact():
    return ModelArtifactIdentity('base','ggml','float16','sha256:'+'a'*64,123,'sha256:'+'b'*64)


def runtime():
    return {name:NativeArtifactIdentity(name,'sha256:'+'c'*64,123)
            for name in ('worker','whisper','ggml-base','ggml-vulkan')}


def profile():
    run=NativeRunPeak(200*MIB,300*MIB,100*MIB,250*MIB,310,True)
    key=native_profile_key(artifact(),runtime(),observation(),threads=2,task='translate')
    return NativeMemoryProfile(key,'base',300,(run,run,run))


def test_native_profile_roundtrip_and_upper_bound():
    p=profile()
    assert NativeMemoryProfile.from_dict(json.loads(json.dumps(p.to_dict()))) == p
    assert p.host_peak_bytes == 500*MIB
    assert p.host_margin_bytes == 512*MIB
    r=native_model_load_requirement(p)
    assert r.required_host_bytes == 1012*MIB
    assert r.provenance == 'native-profile'
    assert r.envelope_resolution is None


@pytest.mark.parametrize('change', [dict(driver_version_raw=9),dict(uuid='2'*32),dict(device_id=9),dict(api_version_raw=9)])
def test_driver_and_device_changes_invalidate_profile(change):
    key=lambda o:native_profile_key(artifact(),runtime(),o,threads=2,task='translate')
    assert key(observation()) != key(observation(**change))


def test_index_and_observation_time_do_not_change_physical_identity():
    key=lambda o:native_profile_key(artifact(),runtime(),o,threads=2,task='translate')
    assert key(observation()) == key(observation(physical_index=7,observed_at=0))


def test_model_runtime_system_and_decoder_settings_are_bound():
    a,r,o=artifact(),runtime(),observation()
    base=native_profile_key(a,r,o,threads=2,task='translate')
    assert native_profile_key(replace(a,weights_sha256='sha256:'+'d'*64),r,o,threads=2,task='translate') != base
    r2=dict(r,worker=NativeArtifactIdentity('worker','sha256:'+'d'*64,123))
    assert native_profile_key(a,r2,o,threads=2,task='translate') != base
    assert native_profile_key(a,r,o,threads=4,task='translate') != base
    assert native_profile_key(a,r,o,threads=2,task='transcribe') != base
    assert native_profile_key(a,r,o,threads=2,task='translate',system=['another OS']) != base


@pytest.mark.parametrize('change', [dict(process_peak_bytes=0),dict(vulkan_peak_bytes=None),dict(released=False),dict(audio_seconds=299)])
def test_missing_evidence_cannot_create_a_run(change):
    with pytest.raises(ValueError):
        replace(profile().runs[0],**change)


def test_three_cold_runs_and_overlap_coverage_required():
    p=profile()
    with pytest.raises(ValueError,match='three cold'):
        replace(p,runs=p.runs[:2])
    with pytest.raises(ValueError,match='extrapolate'):
        replace(p,maximum_chunk_seconds=301)
    with pytest.raises(ValueError):
        NativeMemoryProfile.from_dict(dict(p.to_dict(),unknown=True))


def test_native_requirement_cannot_be_forged_into_cuda_or_fallback():
    r=native_model_load_requirement(profile())
    with pytest.raises(ValueError):
        replace(r,host_incremental_bytes=1)
    with pytest.raises(ValueError):
        replace(r,provenance='envelope')
    with pytest.raises(ValueError):
        replace(model_load_requirement('base'),native_profile=profile())


def test_os_process_peak_is_available_and_positive():
    assert read_process_peak_bytes(os.getpid()) > 0
    with pytest.raises(ValueError):
        read_process_peak_bytes(-1)
