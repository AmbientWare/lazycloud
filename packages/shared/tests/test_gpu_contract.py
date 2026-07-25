from shared.gpu import normalize_gpu_type

CANONICAL_GPU_VALUES = {
    "",
    "any",
    "A10",
    "A10G",
    "A100",
    "A100-40",
    "A100-80",
    "A16",
    "A30",
    "A40",
    "A4000",
    "A5000",
    "A6000",
    "B200",
    "B300",
    "GAUDI2",
    "GH200",
    "H100",
    "H200",
    "L4",
    "L40",
    "L40S",
    "RTX4000Ada",
    "RTX4090",
    "RTX5090",
    "RTX6000",
    "RTX6000Ada",
    "RTXPro6000",
    "T4",
    "V100",
    "V100-32",
}


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
