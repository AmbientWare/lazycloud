from __future__ import annotations

from shared.autoscaling import (
    PodAutoscalerConfig,
    PodAutoscalerSample,
    PodScaleReason,
    PodStubType,
    decide_pod_scale,
)


def test_one_shot_pod_keep_warm_zero_drains_and_minus_one_stays_running() -> None:
    sample = PodAutoscalerSample(current_containers=1)

    immediate = decide_pod_scale(
        sample,
        PodAutoscalerConfig(stub_type=PodStubType.PodRun, keep_warm_seconds=0),
    )
    always_on = decide_pod_scale(
        sample,
        PodAutoscalerConfig(stub_type=PodStubType.PodRun, keep_warm_seconds=-1),
    )

    assert immediate.desired_containers == 0
    assert immediate.reason is PodScaleReason.OneShotKeepWarmDrain
    assert always_on.desired_containers == 1
    assert always_on.reason is PodScaleReason.OneShotRunning
