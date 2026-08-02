"""Phase 3 trainer — QLoRA + ORPO/DPO on a BF16 base, driven entirely by YAML.

    python -m training.train --config training/config/qwen3-30b.yaml

Model-agnostic by construction: base id, target modules and every hyperparameter come
from the config, never from this file. Swapping the served model is a YAML edit.

TRAINS ON PREFERENCE PAIRS ONLY (decision 2026-07-30)
-----------------------------------------------------
Any `kind: "sft"` rows in the dataset are IGNORED and reported as such. Two reasons:

  1. Poisoning. The SFT rows come from `postgres_sessions` — unverified past hive
     outputs. 46 of 287 assert line numbers that nobody checked; the source filters
     catch error envelopes and refusals, but a confidently FABRICATED citation looks
     exactly like a good answer and passes straight through. Training on them would
     teach fluent fabrication while the synthetic pairs teach the opposite — a corpus
     arguing with itself, with the unverified side outnumbering the verified one.

  2. Redundancy. ORPO's objective already applies an SFT cross-entropy term to the
     `chosen` response of every pair, so the preference set carries its own
     regularisation. Separate SFT ballast is unnecessary as well as risky.

To reintroduce SFT later, VERIFY it first (check every line claim against the repo and
drop the ones that do not hold) rather than trusting the source filter.

Run this in the `zgx-train` env ONLY. Installing unsloth into the serving env (`zgx`)
will bump torch out from under vLLM — see AGNOHive 2.3, risk #3 (materialised).
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

import yaml


def load_jsonl(path: str) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def summarise(rows: list[dict]) -> tuple[list[dict], str]:
    """Split the corpus and build an honest, itemised telemetry block.

    Reports the composition of what ACTUALLY trains plus what was skipped, so the log
    can never imply a record contributed when it did not.
    """
    prefs = [r for r in rows if r.get("kind") == "pref"]
    skipped = [r for r in rows if r.get("kind") != "pref"]

    lines = [f"corpus file      : {len(rows)} records"]
    lines.append(f"TRAINING ON     : {len(prefs)} preference pairs (ORPO/DPO)")
    for (src, shape), n in Counter(
        (r["source"], r.get("meta", {}).get("shape", "-")) for r in prefs
    ).most_common():
        label = f"{src}/{shape}" if shape != "-" else src
        lines.append(f"                    {n:5d}  {label}")

    if skipped:
        lines.append(f"IGNORED         : {len(skipped)} non-preference records "
                     f"(sft rows are excluded by design — see module docstring)")
        for src, n in Counter(r["source"] for r in skipped).most_common():
            lines.append(f"                    {n:5d}  {src}")
    return prefs, "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="build the dataset + print the plan, load no weights")
    ap.add_argument("--merge-out", default=None,
                    help="if given, merge to BF16 into this path on the SAME live model "
                         "immediately after training, instead of the normal separate "
                         "training.export.merge step. Required for MoE bases where Unsloth "
                         "auto-attaches LoRA to expert weights via target_parameters (not "
                         "target_modules) — reloading such an adapter from disk to merge it "
                         "crashes with 'Cannot copy out of meta tensor; no data!' because the "
                         "save/reload round-trip does not correctly rematerialize those "
                         "per-expert deltas. Merging on the live in-memory model sidesteps the "
                         "round-trip entirely. Confirmed against qwen3_5_moe (Qwen3.6-35B-A3B) "
                         "2026-08-01; harmless to leave unset for bases without this MoE shape.")
    a = ap.parse_args()

    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    lora, tr = cfg["lora"], cfg["train"]

    rows = load_jsonl(cfg["dataset"])
    prefs, telemetry = summarise(rows)
    print(telemetry)

    if not prefs:
        raise SystemExit("no preference pairs in the corpus — nothing to train on")
    if len(prefs) < 50:
        print("\n  !! WARNING: fewer than 50 preference pairs. A behavioural edit on a 30B")
        print("     model is unlikely to move an eval axis at this volume, and will")
        print("     overfit the phrasing of what little is there. Expand the corpus")
        print("     (training/sources/synthetic_citation.py) before spending a window.\n")

    if a.dry_run:
        print("\n--- DRY RUN — no weights loaded ---")
        print(f"base            : {cfg['base_model']}")
        print(f"objective       : {cfg['objective']}  (implicit SFT on `chosen`)")
        print(f"lora            : r={lora['r']} alpha={lora['alpha']} modules={lora['target_modules']}")
        print(f"lr / epochs     : {tr['learning_rate']} / {tr['num_train_epochs']}")
        print(f"eff. batch      : {tr['per_device_train_batch_size']} x {tr['gradient_accumulation_steps']}"
              f" = {tr['per_device_train_batch_size'] * tr['gradient_accumulation_steps']}")
        print(f"steps/epoch     : ~{len(prefs) // (tr['per_device_train_batch_size'] * tr['gradient_accumulation_steps'])}")
        print(f"output          : {cfg['output_dir']}")
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

    # The ONLY dataset handed to the trainer: prompt / chosen / rejected.
    ds = Dataset.from_list([
        {"prompt": r["prompt"], "chosen": r["chosen"], "rejected": r["rejected"]}
        for r in prefs
    ])
    print(f"[train] dataset -> {len(ds)} rows, columns {ds.column_names}")

    obj = cfg["objective"].lower()
    common = dict(
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

    if obj == "orpo":
        from trl import ORPOConfig, ORPOTrainer
        trainer = ORPOTrainer(model=model, args=ORPOConfig(**common),
                              train_dataset=ds, processing_class=tokenizer)
    elif obj == "dpo":
        from trl import DPOConfig, DPOTrainer
        trainer = DPOTrainer(model=model, ref_model=None, args=DPOConfig(**common),
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

    if a.merge_out:
        t2 = time.time()
        merged_out = Path(a.merge_out)
        merged_out.mkdir(parents=True, exist_ok=True)
        model.save_pretrained_merged(str(merged_out), tokenizer, save_method="merged_16bit")
        size = sum(f.stat().st_size for f in merged_out.rglob("*") if f.is_file()) / 1e9
        print(f"[train] merged BF16 -> {merged_out}  ({size:.1f} GB, {time.time()-t2:.0f}s)")


if __name__ == "__main__":
    main()
