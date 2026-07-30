"""Phase 4a — merge a LoRA adapter into the BF16 base, producing a servable checkpoint.

    python -m training.export.merge --config training/config/qwen3-30b.yaml \
                                    --out /home/abehera1992/models/merged/worker-v1-bf16

Mandatory, not optional: Stage D (2026-07-30) proved vLLM cannot serve a LoRA over an
FP8 base — Triton's `dot` rejects fp8e4nv. Merge + requantise is the ONLY promotion
path, so this step is part of the pipeline rather than a fallback.

The merged BF16 output is what the eval gate scores. Do NOT quantise to FP8 until it
has passed — requantising a failed candidate wastes an hour.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import yaml


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--adapter", default=None, help="defaults to config output_dir")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    adapter = a.adapter or cfg["output_dir"]

    from unsloth import FastLanguageModel

    t0 = time.time()
    # Load at 16-bit: merging into a 4-bit base would bake quantisation error into the
    # weights and make the eval un-comparable to the FP8 that eventually ships.
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=adapter,
        max_seq_length=cfg["train"]["max_seq_length"],
        dtype=None,
        load_in_4bit=False,
        cache_dir=cfg.get("staging_dir"),
    )
    print(f"[merge] adapter + base loaded in {time.time()-t0:.0f}s")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained_merged(str(out), tokenizer, save_method="merged_16bit")

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1e9
    print(f"[merge] merged BF16 -> {out}  ({size:.1f} GB)")
    print("[merge] next: serve this path on a SPARE port and run the eval gate against it.")


if __name__ == "__main__":
    main()
