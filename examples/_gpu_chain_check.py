"""Temporary acceptance probe. Not part of the examples; delete after the run."""

from lazycloud import App, Image

app = App("gpu_chain")
image = Image(python_version="3.12")


# L4 is preferred but T4 is the cheaper card, so where this lands says which
# rule won: the author's order, or the price.
@app.function(image=image, gpu=["l4", "t4"], gpu_count=1, cpu=1, memory="2Gi")
def which_card(_: int = 0) -> dict[str, str]:
    import subprocess

    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {"stdout": out.stdout.strip(), "stderr": out.stderr.strip()[:200]}
