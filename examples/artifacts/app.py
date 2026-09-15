"""Save a report in a volume and attach a downloadable copy to its task.

From the repository root with the SDK installed and signed in:

    lazycloud run examples.artifacts.app:create_report

The function creates or reuses its declared volume. Download the returned
artifact from Storage in the dashboard. The volume copy
stays at ``artifact-reports/report.txt`` until you delete it.
"""

from pathlib import Path

from lazycloud import App, Artifact, Image, Volume

app = App("artifact_storage")
output = Path("/mnt/reports")
reports = Volume("artifact-reports", str(output))


@app.function(image=Image(python_version="3.12"), volumes=[reports], cpu=0.25, memory="256Mi")
def create_report() -> dict[str, str]:
    path = output / "report.txt"
    path.write_text(
        "This report was written through a workspace volume and saved as an artifact.\n"
    )
    saved = Artifact(path=path).save()
    return {"artifact_id": saved.artifact_id, "filename": saved.filename}
