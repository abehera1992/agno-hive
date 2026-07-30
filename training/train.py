"""Phase 3 trainer — QLoRA + ORPO/DPO on a BF16 base, driven entirely by YAML.

    python -m training.train --config training/config/qwen3-30b.yaml

Model-agnostic by construction: base id, target modules and every hyperparameter come
from the config, never from this file. Swapping the served model is a YAML edit.

Run this in the `zgx-train` env ONLY. Installing unsloth into the serving env (`zgx`)
will bump torch out from under vLLM — see AGNOHive 2.3, risk #3 (materialised).
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import yaml


def load_jsonl(path: str) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="build datasets + print the plan, load no weights")
    a = ap.parse_args()

    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    lora, tr = cfg["lora"], cfg["train"]

    rows = load_jsonl(cfg["dataset"])
    prefs = [r for r in rows if r["kind"] == "pref"]
    sfts = [r for r in rows if r["kind"] == "sft"][: cfg.get("max_sft_records", 0)]

    print(f"corpus: {len(rows)} rows -> {len(prefs)} preference, {len(sfts)} sft (capped)")
    if len(prefs) < 50:
        print("\n  !! WARNING: fewer than 50 preference pairs. A behavioural edit on a 30B")
        print("     model is unlikely to move an eval axis at this volume, and will")
        print("     overfit the phrasing of what little is there. Expand the corpus")
        print("     (see training/sources/synthetic_citation.py) before spending a")
        print("     maintenance window on this run.\n")

    if a.dry_run:
        print("\n--- DRY RUN — no weights loaded ---")
        print(f"base            : {cfg['base_model']}")
        print(f"objective       : {cfg['objective']}")
        print(f"lora            : r={lora['r']} alpha={lora['alpha']} modules={lora['target_modules']}")
        print(f"lr / epochs     : {tr['learning_rate']} / {tr['num_train_epochs']}")
        print(f"eff. batch      : {tr['per_device_train_batch_size']} x {tr['gradient_accumulation_steps']}")
        print(f"output          : {cfg['output_dir']}")
        if prefs:
            p = prefs[0]
            print("\nsample pair:")
            print("  prompt  :", p["prompt"][:110].replace("\n", " "))
            print("  chosen  :", p["chosen"][:110].replace("\n", " "))
            print("  rejected:", p["rejected"][:110].replace("\n", " "))
        return

    # Heavy imports only on a real run — unsloth must be imported before transformers.
    from unsloth import FastLanguageModel
    from datasets import Dataset
    import torch

    t0 = time.time()
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=cfg["base_model"],
        max_seq_length=tr["max_seq_length"],
        dtype=None,
        load_in_4bit=cfg.get("load_in_4bit", True),
        cache_dir=cfg.get("staging_dir"),
    )
    print(f"[train] base loaded in {time.time()-t0:.0f}s")

    model = FastLanguageModel.get_peft_model(
        model,
        r=lora["r"], lora_alpha=lora["alpha"], lora_dropout=lora["dropout"],
        bias=lora["bias"], target_modules=lora["target_modules"],
        use_gradient_checkpointing=lora["use_gradient_checkpointing"],
        random_state=tr["seed"],
    )
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"[train] LoRA attached — {trainable:,} trainable params")

    ds = Dataset.from_list([
        {"prompt": r["prompt"], "chosen": r["chosen"], "rejected": r["rejected"]}
        for r in prefs
    ])

    obj = cfg["objective"].lower()
    if obj == "orpo":
        from trl import ORPOConfig, ORPOTrainer
        args = ORPOConfig(
            per_device_train_batch_size=tr["per_device_train_batch_size"],
            gradient_accumulation_steps=tr["gradient_accumulation_steps"],
            learning_rate=tr["learning_rate"],
            lr_scheduler_type=tr["lr_scheduler_type"],
            warmup_ratio=tr["warmup_ratio"],
            num_train_epochs=tr["num_train_epochs"],
            optim=tr["optim"], weight_decay=tr["weight_decay"],
            beta=tr["beta"], bf16=tr["bf16"], seed=tr["seed"],
            logging_steps=tr["logging_steps"], save_strategy=tr["save_strategy"],
            max_length=tr["max_seq_length"],
            output_dir=cfg["output_dir"], report_to="none",
        )
        trainer = ORPOTrainer(model=model, args=args, train_dataset=ds,
                              processing_class=tokenizer)
    elif obj == "dpo":
        from trl import DPOConfig, DPOTrainer
        args = DPOConfig(
            per_device_train_batch_size=tr["per_device_train_batch_size"],
            gradient_accumulation_steps=tr["gradient_accumulation_steps"],
            learning_rate=tr["learning_rate"],
            lr_scheduler_type=tr["lr_scheduler_type"],
            warmup_ratio=tr["warmup_ratio"],
            num_train_epochs=tr["num_train_epochs"],
            optim=tr["optim"], weight_decay=tr["weight_decay"],
            beta=tr["beta"], bf16=tr["bf16"], seed=tr["seed"],
            logging_steps=tr["logging_steps"], save_strategy=tr["save_strategy"],
            max_length=tr["max_seq_length"],
            output_dir=cfg["output_dir"], report_to="none",
        )
        trainer = DPOTrainer(model=model, ref_model=None, args=args,
                             train_dataset=ds, processing_class=tokenizer)
    else:
        raise SystemExit(f"unknown objective {obj!r} (want 'orpo' or 'dpo')")

    t1 = time.time()
    stats = trainer.train()
    print(f"[train] done in {(time.time()-t1)/60:.1f} min | loss {stats.training_loss:.4f}")
    print(f"[train] peak GPU {torch.cuda.max_memory_allocated()/1e9:.1f} GB")

    out = Path(cfg["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(out))
    tokenizer.save_pretrained(str(out))
    print(f"[train] adapter -> {out}")


if __name__ == "__main__":
    main()
