from __future__ import annotations

from collections.abc import Iterable

from benchmarks.harness.models import (
    BenchmarkMeasurement,
    BenchmarkMeasurementName,
    BenchmarkMeasurementStatus,
    BenchmarkValidationFailure,
    BenchmarkValidationFailureKind,
)


class BenchmarkValidator:
    def validate(
        self, measurements: Iterable[BenchmarkMeasurement]
    ) -> tuple[BenchmarkValidationFailure, ...]:
        failures: list[BenchmarkValidationFailure] = []
        for measurement in measurements:
            failures.extend(self._validate_measurement(measurement))
        return tuple(failures)

    def _validate_measurement(
        self, measurement: BenchmarkMeasurement
    ) -> list[BenchmarkValidationFailure]:
        if measurement.status != BenchmarkMeasurementStatus.Ok:
            return [
                self._failure(
                    BenchmarkValidationFailureKind.MeasurementError,
                    measurement,
                    measurement.error or measurement.status.value,
                )
            ]

        policy = measurement.validation
        evidence = measurement.evidence
        failures: list[BenchmarkValidationFailure] = []
        if policy.requires_sha and evidence.sha_ok is not True:
            failures.append(
                self._failure(
                    BenchmarkValidationFailureKind.MissingShaProof,
                    measurement,
                    "required SHA proof was not present",
                )
            )
        if policy.requires_cache_hit and evidence.cache_hit is not True:
            failures.append(
                self._failure(
                    BenchmarkValidationFailureKind.MissingCacheHitProof,
                    measurement,
                    "required embedded-cache hit proof was not present",
                )
            )
        if (
            policy.requires_remote_read
            and evidence.remote_worker is not True
            and evidence.remote_node is not True
        ):
            failures.append(
                self._failure(
                    BenchmarkValidationFailureKind.MissingRemoteReadProof,
                    measurement,
                    "required remote-worker or remote-node proof was not present",
                )
            )
        if policy.reject_cloud_read and evidence.cloud_read is True:
            failures.append(
                self._failure(
                    BenchmarkValidationFailureKind.UnexpectedCloudRead,
                    measurement,
                    "cloud read was observed for a cache-only scenario",
                )
            )
        threshold = self._effective_threshold(measurement)
        if threshold is not None and measurement.mbps < threshold:
            failures.append(
                self._failure(
                    BenchmarkValidationFailureKind.ThroughputBelowThreshold,
                    measurement,
                    f"{measurement.mbps:.2f} MB/s below required {threshold:.2f} MB/s",
                )
            )
        return failures

    @staticmethod
    def _effective_threshold(measurement: BenchmarkMeasurement) -> float | None:
        threshold = measurement.validation.min_mbps
        if threshold is None:
            return None
        if (
            measurement.measurement == BenchmarkMeasurementName.RemoteCacheSocketRead
            and measurement.evidence.network_ceiling_mbps is not None
        ):
            return min(threshold, measurement.evidence.network_ceiling_mbps * 0.9)
        return threshold

    @staticmethod
    def _failure(
        kind: BenchmarkValidationFailureKind,
        measurement: BenchmarkMeasurement,
        message: str,
    ) -> BenchmarkValidationFailure:
        return BenchmarkValidationFailure(
            kind=kind,
            suite=measurement.suite,
            scenario=measurement.scenario,
            measurement=measurement.measurement,
            message=message,
        )
