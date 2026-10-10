"""Train a LoRA adapter on a GPU, resuming from the run's last checkpoint."""

import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from .data import SqlExample, read_examples, require_base_model
from .prompt import chat_messages
from .resources import (
    BASE_MODEL_DIR,
    DATA_DIR,
    DEFAULT_RUN,
    GPUS,
    MAX_LORA_RANK,
    MAX_MODEL_LEN,
    adapter_dir,
    app,
    gpu_image,
    run_dir,
    storage,
)

if TYPE_CHECKING:
    from transformers import PreTrainedTokenizerBase

IGNORED_LABEL = -100
# Sequences per forward pass. Logits over Qwen's 152k-token vocabulary dominate
# GPU memory, so larger passes on long examples overflow a 24 GB card; gradient
# accumulation makes up the rest of batch_size.
DEVICE_BATCH = 2


class TrainingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    epochs: float = Field(default=1.0, gt=0, le=5)
    learning_rate: float = Field(default=2e-4, gt=0, le=1e-3)
    lora_rank: int = Field(default=16, ge=4, le=MAX_LORA_RANK)
    lora_alpha: int = Field(default=32, ge=1, le=256)
    lora_dropout: float = Field(default=0.05, ge=0, lt=1)
    batch_size: int = Field(default=16, ge=DEVICE_BATCH, le=64, multiple_of=DEVICE_BATCH)
    max_tokens: int = Field(default=1024, ge=128, le=MAX_MODEL_LEN)
    save_steps: int = Field(default=50, ge=10)


class TrainingResult(BaseModel):
    run: str
    steps: int
    train_loss: float
    resumed_from: str | None
    minutes: float


@app.function(
    image=gpu_image,
    gpu=GPUS,
    cpu=4,
    memory="24Gi",
    volumes=[storage],
    timeout_seconds=3 * 3600,
    # A failure here would fail again; preemption restarts the call anyway,
    # and the run resumes from its last checkpoint.
    retries=0,
)
def train(run: str = DEFAULT_RUN, settings: Mapping[str, float] | None = None) -> TrainingResult:
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        DataCollatorForSeq2Seq,
        Trainer,
        TrainingArguments,
    )

    started = time.monotonic()
    config = TrainingConfig.model_validate(settings or {})
    path = run_dir(run)
    record_settings(path / "settings.json", config)
    require_base_model()

    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL_DIR)
    examples = read_examples(DATA_DIR / "train.jsonl")
    features = [
        feature
        for example in examples
        if (feature := tokenize(tokenizer, example, config.max_tokens)) is not None
    ]
    print(f"training on {len(features)} of {len(examples)} examples", flush=True)

    model = AutoModelForCausalLM.from_pretrained(BASE_MODEL_DIR, dtype=torch.bfloat16)
    model = get_peft_model(
        model,
        LoraConfig(
            r=config.lora_rank,
            lora_alpha=config.lora_alpha,
            lora_dropout=config.lora_dropout,
            target_modules="all-linear",
            task_type="CAUSAL_LM",
        ),
    )
    checkpoints = path / "checkpoints"
    resume = latest_checkpoint(checkpoints)
    trainer = Trainer(
        model=model,
        args=TrainingArguments(
            output_dir=str(checkpoints),
            num_train_epochs=config.epochs,
            per_device_train_batch_size=DEVICE_BATCH,
            gradient_accumulation_steps=config.batch_size // DEVICE_BATCH,
            learning_rate=config.learning_rate,
            lr_scheduler_type="cosine",
            warmup_steps=0.03,
            bf16=True,
            logging_steps=10,
            save_steps=config.save_steps,
            save_total_limit=2,
            report_to="none",
            disable_tqdm=True,
        ),
        train_dataset=features,
        data_collator=DataCollatorForSeq2Seq(tokenizer, label_pad_token_id=IGNORED_LABEL),
    )
    output = trainer.train(resume_from_checkpoint=str(resume) if resume else None)
    model.save_pretrained(str(adapter_dir(run)))
    return TrainingResult(
        run=run,
        steps=output.global_step,
        train_loss=output.training_loss,
        resumed_from=resume.name if resume else None,
        minutes=round((time.monotonic() - started) / 60, 1),
    )


def tokenize(
    tokenizer: "PreTrainedTokenizerBase", example: SqlExample, max_tokens: int
) -> dict[str, list[int]] | None:
    """Token ids for one example with the loss on the answer only, or None when too long."""
    messages = chat_messages(example.context, example.question)
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    full = tokenizer.apply_chat_template(
        [*messages, {"role": "assistant", "content": example.sql}], tokenize=False
    )
    if not isinstance(prompt, str) or not isinstance(full, str) or not full.startswith(prompt):
        raise ValueError("the chat template does not extend the prompt with the answer")
    prompt_ids: list[int] = tokenizer(prompt, add_special_tokens=False)["input_ids"]
    answer_ids: list[int] = tokenizer(full[len(prompt) :], add_special_tokens=False)["input_ids"]
    input_ids = prompt_ids + answer_ids
    if len(input_ids) > max_tokens:
        return None
    return {
        "input_ids": input_ids,
        "attention_mask": [1] * len(input_ids),
        "labels": [IGNORED_LABEL] * len(prompt_ids) + answer_ids,
    }


def record_settings(path: Path, config: TrainingConfig) -> None:
    """Save a new run's settings, or check that a resumed run keeps them."""
    if path.exists():
        started_with = TrainingConfig.model_validate_json(path.read_text(encoding="utf-8"))
        if started_with != config:
            raise ValueError(
                f"run {path.parent.name} started with {started_with!r}; "
                "resume it with the same settings or choose a new run name"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.model_dump_json(indent=2), encoding="utf-8")


def latest_checkpoint(checkpoints: Path) -> Path | None:
    """The newest checkpoint that finished saving.

    The trainer writes trainer_state.json last, so a save cut short by
    preemption leaves a directory without it.
    """
    complete = [
        directory
        for directory in checkpoints.glob("checkpoint-*")
        if directory.name.removeprefix("checkpoint-").isdigit()
        and (directory / "trainer_state.json").exists()
    ]
    return max(complete, key=lambda d: int(d.name.removeprefix("checkpoint-")), default=None)
