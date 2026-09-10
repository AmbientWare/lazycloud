import pytest
from shared.gpu import (
    SUPPORTED_GPU_TYPES,
    gpu_preference,
    gpu_preference_rank,
    normalize_gpu_type,
)


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

    for gpu in SUPPORTED_GPU_TYPES:
        assert normalize_gpu_type(gpu.value) == gpu.value


def test_a_model_with_two_sizes_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError) as refusal:
        gpu_preference("a100")

    assert "A100-40" in str(refusal.value)
    assert "A100-80" in str(refusal.value)


def test_an_order_is_kept_and_a_wildcard_cannot_hide_what_follows_it() -> None:
    assert gpu_preference(["h100", "l4"]) == ("H100", "L4")
    assert gpu_preference(["h100", "H100", "l4"]) == ("H100", "L4")
    assert gpu_preference_rank(("H100", "L4"), "L4") == 1
    assert gpu_preference_rank(("H100", "L4"), "T4") is None
    # A worker reports its hardware however its driver spells it.
    assert gpu_preference_rank(("A100-40",), "NVIDIA A100-SXM4-40GB") == 0

    with pytest.raises(ValueError):
        gpu_preference(["h100", "any", "l4"])
