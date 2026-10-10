# Fine-tune an LLM for text-to-SQL with LoRA

Train a LoRA adapter for `Qwen/Qwen2.5-Coder-1.5B-Instruct` on SQLite questions,
score it against the base model by running both models' SQL, and serve both
from one vLLM server.

Run these commands from this downloaded project directory:

```bash
uv sync
uv run lazycloud login
uv run lazycloud deploy lora_sql.app:app
uv run lazycloud run lora_sql.data:prepare_data
uv run lazycloud run lora_sql.train:train
uv run lazycloud run lora_sql.evaluate:evaluate
```

`evaluate` prints the base and tuned accuracy and a link to the Markdown
report. To try the server, create an access token in the dashboard. The
configure script stores it with the `sql-server` URL from the deploy output,
the one whose host ends in `-8000`. Then ask a question:

```bash
uv run python -m lora_sql.configure
uv run python -m lora_sql.ask "Which customers spent more than 100 in total?"
```

Training and generation run on an L4 or A10G GPU. The `sql-server` pod stops
after five idle minutes. To remove everything:

```bash
uv run lazycloud app delete lora_sql
uv run lazycloud volume delete lora-sql
uv run lazycloud secret delete SQL_SERVER_URL
uv run lazycloud secret delete SQL_SERVER_TOKEN
```

Then delete the access token in the dashboard.

[Full guide](https://docs.lazycloud.dev/examples/lora-fine-tune)

## Keep your project reproducible

`pyproject.toml` pins the SDK version that supplied this example. `uv sync`
creates the local environment and `uv.lock`. Commit the lockfile with your code;
use `uv sync --locked` in CI. Keep credentials out of source control.
Remote GPU and system dependencies are defined in the workload's image.

Downloading and installing the project creates no cloud resources. Running or
deploying workloads can incur charges. Inspect CLI logs when a run fails.
