import pytest
from provider_aws.storage_access import parse_access_log
from shared.storage_access import StorageRequestClass, StorageTransferEvidence


def test_access_observation_uses_response_bytes_and_discards_signed_request() -> None:
    line = (
        "canonical bucket [06/Feb/2026:00:00:38 +0000] 192.0.2.3 principal request123 "
        "REST.GET.OBJECT workspaces/id/artifacts/file "
        '"GET /file?X-Amz-Signature=sensitive-signature HTTP/1.1" '
        '206 - 4096 262144 20 10 "-" "client" - hostid SigV4 cipher QueryString '
        "bucket.s3.us-east-1.amazonaws.com TLSv1.2 - - -"
    )
    observed = parse_access_log(line, region="us-east-1")
    assert observed.response_bytes == 4096
    assert observed.request_class is StorageRequestClass.Read
    assert observed.transfer_evidence is StorageTransferEvidence.Unknown
    assert "sensitive-signature" not in observed.model_dump_json()
    assert "X-Amz" not in observed.model_dump_json()
    with pytest.raises(ValueError, match="format"):
        parse_access_log(line.replace('HTTP/1.1"', 'HTTP/1.1" "forged"'), region="us-east-1")
