from datetime import UTC, datetime

import pytest
from provider_aws.retained_pool import sleep_image_evidence
from shared.capacity_lifecycle import CapacityImageEvidence, CapacitySleepMode, CapacitySleepRequest


@pytest.mark.parametrize(
    ("transcript", "evidence"),
    [
        (
            "{marker}\nPM: hibernation: hibernation entry\n"
            "PM: hibernation: Saving image data pages (123 pages)...\n"
            "PM: hibernation: Image saving done\n",
            CapacityImageEvidence.Saved,
        ),
        (
            "{marker}\nPM: hibernation: hibernation entry\n"
            "PM: hibernation: Compressing and saving image data (123 pages)...\n"
            "PM: hibernation: Image saving done\n"
            "PM: hibernation: hibernation exit\n",
            CapacityImageEvidence.Failed,
        ),
        (
            "PM: hibernation: hibernation entry\n"
            "PM: hibernation: Saving image data pages (123 pages)...\n"
            "PM: hibernation: Image saving done\n{marker}\n",
            CapacityImageEvidence.Unknown,
        ),
        (
            "{marker}\nPM: hibernation: hibernation entry\n"
            "PM: hibernation: Saving image data pages (123 pages)...\n"
            "PM: hibernation: Image saving done\nLinux version 6.1.0\n",
            CapacityImageEvidence.Unknown,
        ),
        (
            "{marker}\nPM: hibernation: hibernation entry\n"
            "Freezing remaining freezable tasks failed after 20.001 seconds\n",
            CapacityImageEvidence.Failed,
        ),
        (
            "{marker}\nPM: hibernation: Image saving done\n",
            CapacityImageEvidence.Unknown,
        ),
    ],
)
def test_only_a_complete_current_kernel_save_sequence_confirms_image_evidence(
    transcript: str, evidence: CapacityImageEvidence
) -> None:
    request = CapacitySleepRequest(
        attempt_id="11111111-1111-4111-8111-111111111111",
        boot_id="22222222-2222-4222-8222-222222222222",
        mode=CapacitySleepMode.Hibernate,
        requested_at=datetime.now(UTC),
    )
    marker = f"lazycloud-sleep attempt={request.attempt_id} boot={request.boot_id}"
    assert sleep_image_evidence(transcript.format(marker=marker), request) is evidence
