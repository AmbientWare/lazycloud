"""Probe functions the scheduler calls every six hours to time first calls.

Deploy into a workspace the operator owns, then set the scheduler's
LAZYCLOUD_SYNTHETIC_APP to <workspace id>/synthetic. Each probe returns at
once, keeps no container warm and runs on capacity that is not preempted, so
a call starts on a warm host or resumes a reserve.
"""

from lazycloud import App, GpuType

app = App("synthetic")


@app.function(cpu=1, memory="512Mi", preemptible=False, retries=0, keep_warm=0)
def cpu_1() -> None:
    return None


@app.function(cpu=16, memory="1Gi", preemptible=False, retries=0, keep_warm=0)
def cpu_16() -> None:
    return None


@app.function(gpu=GpuType.T4, memory="1Gi", preemptible=False, retries=0, keep_warm=0)
def gpu_t4() -> None:
    return None
