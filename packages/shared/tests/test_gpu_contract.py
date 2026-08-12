from shared.gpu import SUPPORTED_GPU_TYPES, normalize_gpu_type


def test_gpu_normalization_preserves_no_gpu_any_and_overlapping_aliases() -> None:
    assert normalize_gpu_type("") == ""
    assert normalize_gpu_type("0") == ""
    assert normalize_gpu_type("none") == ""
    assert normalize_gpu_type("CPU") == ""
    assert normalize_gpu_type("ANY") == "any"
    assert normalize_gpu_type("NVIDIA A100 80GB PCI") == "A100-80"
    assert normalize_gpu_type("NVIDIA RTX 6000 Ada") == "RTX6000Ada"
    assert normalize_gpu_type("Tesla V100 32GB") == "V100-32"
    assert normalize_gpu_type("L40S") == "L40S"
    assert normalize_gpu_type("A10G") == "A10G"


def test_gpu_normalization_keeps_unknown_provider_hardware_as_reported() -> None:
    assert normalize_gpu_type("Future Accelerator X") == "Future Accelerator X"


def test_every_supported_model_is_already_its_own_normalized_form() -> None:
    """A supported name has to survive the normalisation a worker's report goes through.

    The scheduler matches a request against what the worker on the machine says it
    has, normalised on the way. A supported name that normalises to something else
    is hardware that provisions and then never receives work — and, since GPU
    seconds are priced per model, bills under a name no rate answers to.
    """

    for gpu in SUPPORTED_GPU_TYPES:
        assert normalize_gpu_type(gpu.value) == gpu.value
