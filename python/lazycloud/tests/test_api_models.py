import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_api_models_match_the_openapi_contract() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "datamodel_code_generator", "--profile", "api", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
