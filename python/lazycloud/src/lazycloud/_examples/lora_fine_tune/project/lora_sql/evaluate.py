"""Score the base model and the adapter on held-out questions by running their SQL."""

import tempfile
from collections import defaultdict
from collections.abc import Sequence
from itertools import batched
from pathlib import Path

from lazycloud import Artifact, Autoscaler
from pydantic import BaseModel

from .data import SqlExample, read_examples, require_base_model
from .prompt import chat_messages, extract_sql
from .queries import execution_match
from .resources import (
    BASE_MODEL_DIR,
    DATA_DIR,
    DEFAULT_RUN,
    GPUS,
    MAX_LORA_RANK,
    MAX_MODEL_LEN,
    MAX_SQL_TOKENS,
    TUNED_MODEL_NAME,
    adapter_dir,
    app,
    cpu_image,
    gpu_image,
    storage,
)

SCORING_BATCH = 50
REPORT_DAYS = 7
SHOWN_EXAMPLES = 5


class Predictions(BaseModel):
    base: list[str]
    tuned: list[str]


class ScoringCase(BaseModel):
    context: str
    reference_sql: str
    predicted_sql: str


class EvaluationResult(BaseModel):
    run: str
    questions: int
    base_accuracy: float
    tuned_accuracy: float
    report_url: str


@app.function(
    image=gpu_image,
    gpu=GPUS,
    cpu=4,
    memory="16Gi",
    volumes=[storage],
    timeout_seconds=1800,
    retries=1,
)
def generate_sql(run: str, examples: list[SqlExample]) -> Predictions:
    """Answer every question twice in one vLLM engine: base weights, then base plus adapter."""
    from vllm import LLM, SamplingParams
    from vllm.lora.request import LoRARequest

    require_base_model()
    adapter = adapter_dir(run)
    if not (adapter / "adapter_config.json").exists():
        raise FileNotFoundError(f"run {run} has no adapter; run lora_sql.train:train first")
    llm = LLM(
        model=str(BASE_MODEL_DIR),
        enable_lora=True,
        max_lora_rank=MAX_LORA_RANK,
        max_model_len=MAX_MODEL_LEN,
    )
    conversations = [chat_messages(example.context, example.question) for example in examples]
    greedy = SamplingParams(temperature=0, max_tokens=MAX_SQL_TOKENS)
    base = llm.chat(conversations, greedy, use_tqdm=False)
    tuned = llm.chat(
        conversations,
        greedy,
        use_tqdm=False,
        lora_request=LoRARequest(TUNED_MODEL_NAME, 1, str(adapter)),
    )
    return Predictions(
        base=[extract_sql(output.outputs[0].text) for output in base],
        tuned=[extract_sql(output.outputs[0].text) for output in tuned],
    )


@app.function(
    image=cpu_image,
    cpu=0.25,
    memory="256Mi",
    timeout_seconds=300,
    retries=2,
    max_pending_tasks=200,
    autoscaler=Autoscaler(max_containers=10, tasks_per_container=1),
)
def score_batch(cases: list[ScoringCase]) -> list[bool]:
    return [execution_match(case.context, case.reference_sql, case.predicted_sql) for case in cases]


@app.function(
    image=cpu_image,
    volumes=[storage],
    cpu=0.25,
    memory="512Mi",
    timeout_seconds=3600,
    retries=0,
)
def evaluate(run: str = DEFAULT_RUN, limit: int = 500) -> EvaluationResult:
    if not 1 <= limit <= 2_000:
        raise ValueError("limit must be between 1 and 2000")
    examples = read_examples(DATA_DIR / "eval.jsonl")[:limit]
    predictions = generate_sql.remote(run, examples)
    correct = score(
        [*scoring_cases(examples, predictions.base), *scoring_cases(examples, predictions.tuned)]
    )
    base_correct, tuned_correct = correct[: len(examples)], correct[len(examples) :]

    report = build_report(run, examples, predictions, base_correct, tuned_correct)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"evaluation-{run}.md"
        path.write_text(report, encoding="utf-8")
        artifact = Artifact.file(path, content_type="text/markdown")
        artifact.save()
        report_url = artifact.public_url(expires=REPORT_DAYS * 24 * 3600)
    return EvaluationResult(
        run=run,
        questions=len(examples),
        base_accuracy=accuracy(base_correct),
        tuned_accuracy=accuracy(tuned_correct),
        report_url=report_url,
    )


def scoring_cases(examples: Sequence[SqlExample], predicted: Sequence[str]) -> list[ScoringCase]:
    return [
        ScoringCase(context=example.context, reference_sql=example.sql, predicted_sql=sql)
        for example, sql in zip(examples, predicted, strict=True)
    ]


def score(cases: Sequence[ScoringCase]) -> list[bool]:
    """Run each predicted query against its database, in parallel batches."""
    results = list(score_batch.map([(list(batch),) for batch in batched(cases, SCORING_BATCH)]))
    failed = sum(result is None for result in results)
    if failed:
        raise RuntimeError(f"{failed} of {len(results)} scoring batches failed")
    return [correct for result in results if result is not None for correct in result]


def accuracy(correct: Sequence[bool]) -> float:
    return round(sum(correct) / len(correct), 4) if correct else 0.0


def build_report(
    run: str,
    examples: Sequence[SqlExample],
    predictions: Predictions,
    base_correct: Sequence[bool],
    tuned_correct: Sequence[bool],
) -> str:
    """A Markdown report: overall and per-complexity accuracy, then sample answers."""
    by_complexity: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    for example, base, tuned in zip(examples, base_correct, tuned_correct, strict=True):
        by_complexity[example.complexity].append((base, tuned))

    lines = [
        f"# Execution accuracy for run `{run}`",
        "",
        f"{len(examples)} held-out questions. A query is correct when SQLite returns "
        "the same rows as the reference query.",
        "",
        "| Questions | Count | Base | Tuned |",
        "| --- | ---: | ---: | ---: |",
        f"| All | {len(examples)} | {accuracy(base_correct):.1%} | {accuracy(tuned_correct):.1%} |",
    ]
    for complexity, pairs in sorted(by_complexity.items()):
        base_rate = accuracy([base for base, _ in pairs])
        tuned_rate = accuracy([tuned for _, tuned in pairs])
        lines.append(f"| {complexity} | {len(pairs)} | {base_rate:.1%} | {tuned_rate:.1%} |")

    rows = list(
        zip(examples, predictions.base, predictions.tuned, base_correct, tuned_correct, strict=True)
    )
    sections = [
        ("Fixed by the adapter", [row for row in rows if row[4] and not row[3]]),
        ("Still wrong after tuning", [row for row in rows if not row[4]]),
    ]
    for title, selected in sections:
        lines += ["", f"## {title}"]
        for example, base_sql, tuned_sql, _, _ in selected[:SHOWN_EXAMPLES]:
            lines += [
                "",
                f"**{example.question}**",
                "",
                f"Reference:\n\n```sql\n{example.sql}\n```",
                f"Base:\n\n```sql\n{base_sql}\n```",
                f"Tuned:\n\n```sql\n{tuned_sql}\n```",
            ]
    return "\n".join(lines) + "\n"
