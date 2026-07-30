"""Phase 4 — quantise a merged BF16 checkpoint to FP8 for serving.

Runs INSIDE the vLLM container (which already has the aarch64/CUDA13 torch stack and
compressed-tensors), not in zgx-train:

    docker run --rm --device nvidia.com/gpu=all \
      -v <bf16>:/model:ro -v <outdir>:/out -v <repo>:/repo \
      --entrypoint bash timothystewart6/vllm-gb10:latest -c \
      "pip install -q llmcompressor && python /repo/training/export/quantise.py --model /model --out /out"

Why FP8 at all: the merged BF16 is ~61 GB. vllm-coord serves at
--gpu-memory-utilization 0.6 of a 121 GB unified pool (~72 GB) with
--max-model-len 262144. BF16 weights would leave ~11 GB for KV cache at 256K context,
which does not fit; FP8 (~31 GB) leaves ~41 GB. The quantisation is a serving
requirement, not an optimisation.

WHAT MUST NOT BE QUANTISED (get this wrong and quality craters silently):
  * lm_head — standard exclusion; output logits are sensitive to weight error.
  * the MoE ROUTER, `model.layers.N.mlp.gate`. It is a Linear, so `targets="Linear"`
    catches it by default. The router picks which of 128 experts run; an FP8 rounding
    error there does not blur an activation, it routes the token to a DIFFERENT expert.
    The regex below anchors on `.gate` so it excludes the router while leaving the
    expert projections `.gate_proj` (a different module) quantised.

FP8_DYNAMIC is weight-only + dynamic activation scales, so it needs NO calibration
dataset — there is no data-selection judgement baked into the result.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

# `mlp.gate` = MoE router (EXCLUDE). `mlp.gate_proj` = expert projection (quantise).
# The `$` anchor is what separates them; dropping it would silently exclude every
# expert gate projection and inflate the checkpoint.
DEFAULT_IGNORE = ["lm_head", r"re:.*mlp\.gate$"]


def du(p: str | Path) -> str:
    try:
        return subprocess.run(["du", "-sh", str(p)], capture_output=True, text=True
                              ).stdout.split()[0]
    except Exception:
        return "?"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="merged BF16 checkpoint dir")
    ap.add_argument("--out", required=True, help="FP8 output dir")
    ap.add_argument("--ignore", nargs="*", default=None,
                    help="modules to leave unquantised (default: lm_head + MoE router)")
    ap.add_argument("--check", action="store_true",
                    help="resolve imports and build the recipe, then exit without loading "
                         "the model. Run this BEFORE opening a maintenance window: it "
                         "exercises the real file in the real container, so a stale "
                         "checkout or a moved API fails in seconds with hive still up "
                         "instead of 60s into a window (which is how 2026-07-30's first "
                         "attempt aborted — /repo was one commit behind the import fix).")
    a = ap.parse_args()

    ignore = a.ignore if a.ignore is not None else DEFAULT_IGNORE
    print(f"[quantise] in  : {a.model}  ({du(a.model)})")
    print(f"[quantise] out : {a.out}")
    print(f"[quantise] ignore: {ignore}")

    from llmcompressor.modifiers.quantization import QuantizationModifier
    try:
        # llmcompressor >= 0.4 exposes oneshot at the top level.
        from llmcompressor import oneshot
    except ImportError:  # pragma: no cover - only hit if pip resolves an old release
        # <= 0.3 kept it under .transformers. Pinning compressed-tensors==0.17.0 to match
        # vLLM's pin makes pip resolve BACKWARDS to 0.3.0, so this path is reachable by
        # accident; verified 2026-07-30. Let llmcompressor bring its own
        # compressed-tensors (0.17.1) instead — the on-disk FP8 format is unchanged at
        # patch level and the serving container still loads it with 0.17.0.
        from llmcompressor.transformers import oneshot

    recipe = QuantizationModifier(targets="Linear", scheme="FP8_DYNAMIC", ignore=ignore)

    if a.check:
        print(f"[quantise] --check OK: oneshot resolved from {oneshot.__module__}, "
              f"recipe {type(recipe).__name__} built. Not loading the model.")
        return

    oneshot(model=a.model, recipe=recipe, output_dir=a.out)

    out = Path(a.out)
    shards = sorted(out.glob("*.safetensors"))
    print(f"[quantise] wrote {len(shards)} shard(s) -> {a.out}  ({du(a.out)})")
    if not shards:
        raise SystemExit("[quantise] FAILED: no safetensors written")

    # A correct FP8 run must leave a quantization_config behind; without it vLLM would
    # silently load the checkpoint as unquantised and the memory maths above collapses.
    cfg = (out / "config.json").read_text(encoding="utf-8")
    if "quantization_config" not in cfg:
        raise SystemExit("[quantise] FAILED: config.json has no quantization_config — "
                         "vLLM would load this as unquantised BF16.")
    print("[quantise] config.json carries quantization_config — OK")


if __name__ == "__main__":
    main()
