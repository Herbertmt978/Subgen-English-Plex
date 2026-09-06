"""Aggregate existing pressure controllers for one fixed-model GPU cohort.

Resource thresholds, hysteresis and model requirements remain canonical in
resource_management. A cached bundle supplies ONE host observation to every
device; cold/recovery admission still checks the combined working set once.
This owns neither workers, model memory reservations nor the library queue.
"""
from dataclasses import replace
import threading
import time

from .cohort_runtime import CohortReleaseError
from .resource_management import (PressureSample, PressureController,
    evaluate_cohort_admission)


class CohortPressureController:
    def __init__(self, requests, *, sample_reader, host_reserve_bytes,
                 staging_reserve_bytes, release_verified, check_cancelled,
                 require_cgroup=False, priority_reader=None,
                 clock=time.monotonic, sleep=time.sleep):
        if not all(callable(v) for v in (sample_reader, release_verified, check_cancelled, clock, sleep)):
            raise TypeError('Cohort pressure dependencies must be callable')
        # Reuse canonical shape/identity/reserve validation without reading or
        # allocating anything. An unavailable sample simply refuses admission.
        evaluate_cohort_admission(requests, PressureSample(observed_at=clock()),
            host_reserve_bytes=host_reserve_bytes, staging_reserve_bytes=staging_reserve_bytes,
            require_cgroup=require_cgroup, now=clock())
        self._requests = requests
        self._reader, self._clock, self._sleep = sample_reader, clock, sleep
        self._release_verified, self._check_cancelled = release_verified, check_cancelled
        self._host_reserve, self._staging_reserve = host_reserve_bytes, staging_reserve_bytes
        self._require_cgroup = require_cgroup
        self._lock = threading.RLock()
        self._bundle = None
        self._read_at = None
        self._read_failed = False
        self.last_admission = None
        self.last_reasons = ()
        self._controllers = tuple(PressureController(
            sample_reader=lambda index=index: self._merged(index),
            reserve_bytes=host_reserve_bytes,
            gpu_reserve_bytes=(request.device_reserve_bytes
                               if request.device.memory_topology == 'dedicated' else None),
            expected_gpu_device=(request.device.physical_uuid
                                 if request.device.memory_topology == 'dedicated' else None),
            require_gpu_telemetry=request.device.memory_topology == 'dedicated',
            selected_requirement=request.requirement, require_cgroup=require_cgroup,
            priority_reader=priority_reader if index == 0 else None,
            clock=clock, sleep=sleep)
            for index, request in enumerate(requests))

    def _read(self):
        now = self._clock()
        if self._read_at is not None and now < self._read_at:
            raise ValueError('Cohort telemetry clock moved backwards')
        if (self._read_at is None
                or now-self._read_at >= self._controllers[0].poll_interval_seconds):
            # Cache failed attempts too. Never reuse the previous healthy bundle
            # after a failed refresh or retain a probe exception's traceback.
            self._read_at, self._bundle, self._read_failed = now, None, True
            bundle = self._reader()
            if (not isinstance(bundle, tuple) or len(bundle) != 2
                    or type(bundle[0]) is not PressureSample or not isinstance(bundle[1], tuple)
                    or len(bundle[1]) != len(self._requests)
                    or any(type(sample) is not PressureSample for sample in bundle[1])):
                raise ValueError('Cohort telemetry needs one host and one sample per selected worker')
            self._bundle, self._read_failed = bundle, False
        if self._read_failed:
            raise OSError('Cohort telemetry is unavailable until the next sample')
        return self._bundle

    def _merged(self, index):
        host, gpus = self._read()
        sample = gpus[index]
        dedicated = self._requests[index].device.memory_topology == 'dedicated'
        # Ignore any host fields smuggled through a per-device observation.
        return replace(host,
            gpu_total_bytes=sample.gpu_total_bytes if dedicated else None,
            gpu_free_bytes=sample.gpu_free_bytes if dedicated else None,
            gpu_device_id=sample.gpu_device_id if dedicated else None,
            gpu_observed_at=sample.gpu_observed_at if dedicated else None,
            priority_observation=host.priority_observation if index == 0 else None)

    def _admission(self):
        host, gpus = self._read()
        requests = tuple(replace(request, gpu_sample=sample)
                         for request, sample in zip(self._requests, gpus))
        self.last_admission = evaluate_cohort_admission(requests, host,
            host_reserve_bytes=self._host_reserve, staging_reserve_bytes=self._staging_reserve,
            require_cgroup=self._require_cgroup, now=self._clock())
        return self.last_admission

    def _poll(self, *, resident):
        decision = self._admission()
        # An already loaded model must not be asked to fit a SECOND model copy.
        # Keep availability/integrity failures, not cold capacity refusals.
        reasons = [r for r in decision.reasons
                   if not r.rsplit(':', 1)[-1].startswith('insufficient_')
                   and r.rsplit(':', 1)[-1] != 'gpu_unavailable']
        for request, controller in zip(self._requests, self._controllers):
            controller.poll(model_resident=resident, inference_active=resident)
            if controller.should_yield:
                details = controller.last_critical_reasons or controller.last_pressure_reasons
                reasons.extend(f'{request.device.selector}:{reason}' for reason in
                               (details or (controller.recovery_reason or 'resource_pressure',)))
        self.last_reasons = tuple(dict.fromkeys(reasons))
        return decision

    def check_healthy(self):
        self._check_cancelled()
        with self._lock:
            try:
                self._poll(resident=True)
            except (OSError, ValueError, TypeError):
                self.last_reasons = ('telemetry_unavailable',)
            return not self.last_reasons

    def decide_admission(self):
        self._check_cancelled()
        with self._lock:
            decision = self._poll(resident=False)
            reasons = tuple(dict.fromkeys(decision.reasons + self.last_reasons))
            if any(not controller.admission_open for controller in self._controllers):
                reasons += ('resource_recovery',)
            return replace(decision, admitted=not reasons, reasons=reasons)

    def wait_for_recovery(self, _error):
        """Only a verified release permits recovery sampling and another load."""
        self._check_cancelled()
        if self._release_verified() is not True:
            raise CohortReleaseError('Cohort memory release is unconfirmed; recovery is blocked')
        with self._lock:
            before = sum(c.external_pressure_recovery_generation for c in self._controllers)
            for controller in self._controllers:
                controller.mark_released()
        healthy_samples, last_observed = 0, None
        while True:
            self._check_cancelled()
            with self._lock:
                try:
                    decision = self._poll(resident=False)
                    observed = self._bundle[0].observed_at
                    distinct = last_observed is None or observed > last_observed
                    if distinct:
                        last_observed = observed
                        qualified = decision.admitted and not self.last_reasons
                        healthy_samples = healthy_samples+1 if qualified else 0
                    if (healthy_samples >= self._controllers[0].recovery_sample_count
                            and all(c.state == c.NORMAL and c.admission_open for c in self._controllers)):
                        after = sum(c.external_pressure_recovery_generation for c in self._controllers)
                        return before, after
                except (OSError, ValueError, TypeError):
                    self.last_reasons = ('telemetry_unavailable',)
                    healthy_samples = 0
            self._sleep(self._controllers[0].poll_interval_seconds)
