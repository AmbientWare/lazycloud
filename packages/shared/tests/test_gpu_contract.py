import pytest
from shared.gpu import (
    GPU_ANY,
    SUPPORTED_GPU_NAMES,
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
    """A supported name has to survive the normalisation a worker's report goes through.

    The scheduler matches a request against what the worker on the machine says it
    has, normalised on the way. A supported name that normalises to something else
    is hardware that provisions and then never receives work — and, since GPU
    seconds are priced per model, bills under a name no rate answers to.
    """

    for gpu in SUPPORTED_GPU_TYPES:
        assert normalize_gpu_type(gpu.value) == gpu.value


def test_every_gpu_the_docs_offer_can_actually_be_scheduled() -> None:
    """The documentation is a contract, and this is the one it broke.

    `gpu="a100"` was advertised for months. It normalises to a real vocabulary
    member, so nothing rejected it, and no worker ever reports it, because a
    card is a 40GB or an 80GB one. The workload placed nowhere and said
    `offer_unavailable` at deploy time, hours from the line that caused it.
    """
    documented = ("t4", "l4", "a10g", "a100-40", "a100-80", "l40s", "h100", "h200", "any")

    for name in documented:
        preference = gpu_preference(name)
        assert preference, name
        assert set(preference) <= SUPPORTED_GPU_NAMES | {GPU_ANY}, name


def test_a_model_with_two_sizes_is_refused_rather_than_guessed() -> None:
    """Neither size is the safe default, so the platform declines to pick one.

    Choosing the smaller OOMs a model that needed the larger; choosing the
    larger doubles a bill nobody agreed to. The refusal names both.
    """
    with pytest.raises(ValueError) as refusal:
        gpu_preference("a100")

    assert "A100-40" in str(refusal.value)
    assert "A100-80" in str(refusal.value)


def test_an_order_is_kept_and_a_wildcard_cannot_hide_what_follows_it() -> None:
    """The order is the instruction, and `any` swallows whatever comes after."""
    assert gpu_preference(["h100", "l4"]) == ("H100", "L4")
    assert gpu_preference(["h100", "H100", "l4"]) == ("H100", "L4")
    assert gpu_preference_rank(("H100", "L4"), "L4") == 1
    assert gpu_preference_rank(("H100", "L4"), "T4") is None
    # A worker reports its hardware however its driver spells it.
    assert gpu_preference_rank(("A100-40",), "NVIDIA A100-SXM4-40GB") == 0

    with pytest.raises(ValueError):
        gpu_preference(["h100", "any", "l4"])
