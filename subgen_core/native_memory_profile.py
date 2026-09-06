"""Native calibration evidence, deliberately separate from OCI/CUDA catalogs.

Profiles match one installed native stack and physical driver/device. The
shared-RAM upper bound includes process memory and Vulkan allocations; it is
not a claim that every driver allocation is charged to a process or cgroup.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import platform
import re

from .model_envelope_catalog import ModelArtifactIdentity, NativeArtifactIdentity
from .vulkan_probe import VulkanDeviceObservation

MIB = 1024**2
SCHEMA = 'subgen.native-memory-profile/v1'


def _bytes(value, *, positive=False):
    if type(value) is not int or not (1 if positive else 0) <= value < 2**50:
        raise ValueError('Native calibration requires bounded byte counts')
    return value


def native_profile_key(artifact, artifacts, observation, *, threads, task, system=None):
    if type(artifact) is not ModelArtifactIdentity or artifact.backend_format != 'ggml':
        raise ValueError('Native calibration requires a GGML artifact')
    artifact.__post_init__()
    if type(observation) is not VulkanDeviceObservation or observation.memory_topology != 'shared':
        raise ValueError('Native shared-RAM calibration requires a shared Vulkan GPU')
    if type(threads) is not int or not 1 <= threads <= 256 or task not in ('transcribe','translate'):
        raise ValueError('Native calibration decoder settings are invalid')
    values = tuple(artifacts.values())
    if not 4 <= len(values) <= 32 or any(type(v) is not NativeArtifactIdentity for v in values):
        raise ValueError('Native calibration requires a complete runtime identity')
    for value in values:
        value.__post_init__()
    if len({v.component for v in values}) != len(values):
        raise ValueError('Native calibration repeats a runtime component')
    fields = ('uuid','vendor_id','device_id','driver_version_raw','api_version_raw','memory_topology')
    identity = dict(model=asdict(artifact), runtime=[asdict(v) for v in sorted(values,key=lambda v:v.component)],
                    device={field:getattr(observation,field) for field in fields},
                    system=system or [platform.system(),platform.release(),platform.machine()],
                    threads=threads, task=task, language='auto')
    return 'sha256:'+hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()


@dataclass(frozen=True)
class NativeRunPeak:
    process_peak_bytes: int
    vulkan_peak_bytes: int
    host_incremental_peak_bytes: int
    cgroup_incremental_peak_bytes: int
    audio_seconds: int
    released: bool

    def __post_init__(self):
        for field in ('process_peak_bytes','vulkan_peak_bytes'):
            _bytes(getattr(self,field),positive=True)
        for field in ('host_incremental_peak_bytes','cgroup_incremental_peak_bytes'):
            _bytes(getattr(self,field))
        if type(self.audio_seconds) is not int or not 300 <= self.audio_seconds <= 1810 or self.released is not True:
            raise ValueError('Native calibration needs a completed bounded chunk and verified release')

    @property
    def host_upper_bound_bytes(self):
        # RSS/commit can overlap mapped driver memory on some systems. Adding
        # the two gives an intentional upper bound instead of assuming that
        # the OS charged every Vulkan allocation. The cohort counts it once.
        return max(self.process_peak_bytes+self.vulkan_peak_bytes,
                   self.host_incremental_peak_bytes,self.cgroup_incremental_peak_bytes)


@dataclass(frozen=True)
class NativeMemoryProfile:
    key: str
    model: str
    maximum_chunk_seconds: int
    runs: tuple

    def __post_init__(self):
        if not isinstance(self.key,str) or re.fullmatch(r'sha256:[a-f0-9]{64}',self.key) is None:
            raise ValueError('Native calibration requires an exact identity digest')
        if self.model not in {'tiny','tiny.en','base','base.en','small','small.en','medium','medium.en','large-v1','large-v2','large-v3'}:
            raise ValueError('Native calibration model is unsupported')
        if type(self.maximum_chunk_seconds) is not int or not 300 <= self.maximum_chunk_seconds <= 1800:
            raise ValueError('Native calibration chunk bound is invalid')
        if not isinstance(self.runs,tuple) or not 3 <= len(self.runs) <= 16:
            raise ValueError('Native calibration requires at least three cold runs')
        for run in self.runs:
            if type(run) is not NativeRunPeak:
                raise ValueError('Native calibration run is invalid')
            run.__post_init__()
            if run.audio_seconds < self.maximum_chunk_seconds + 10:
                raise ValueError('Native calibration cannot extrapolate beyond tested chunk length')

    @property
    def host_peak_bytes(self):
        return max(r.host_upper_bound_bytes for r in self.runs)

    @property
    def device_peak_bytes(self):
        return max(r.vulkan_peak_bytes for r in self.runs)

    @property
    def host_margin_bytes(self):
        return max(512*MIB,(self.host_peak_bytes+3)//4)

    @property
    def device_margin_bytes(self):
        return max(512*MIB,(self.device_peak_bytes+3)//4)

    def to_dict(self):
        return dict(schema=SCHEMA,**asdict(self))

    @classmethod
    def from_dict(cls,data):
        if not isinstance(data,dict) or set(data)!={'schema','key','model','maximum_chunk_seconds','runs'} or data['schema']!=SCHEMA:
            raise ValueError('Native calibration schema is invalid')
        if not isinstance(data['runs'],list) or len(data['runs'])>16:
            raise ValueError('Native calibration run list is invalid')
        try:
            return cls(data['key'],data['model'],data['maximum_chunk_seconds'],tuple(NativeRunPeak(**r) for r in data['runs']))
        except TypeError:
            raise ValueError('Native calibration contains missing or unknown fields') from None
