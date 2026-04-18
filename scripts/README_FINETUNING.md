# Fine-Tuning a Local Code LLM on CadQuery Synthesis

This directory contains scripts to generate a synthetic dataset and fine-tune
a local model to replace Anthropic-dependent complex-CAD generation in ARIA-OS.

---

## 1. Generate the synthetic dataset

```bash
# Default: 50 sweeps per template, writes to outputs/datasets/synthetic_cad_<ts>.jsonl
python scripts/build_synthetic_dataset.py

# Smaller run for testing
python scripts/build_synthetic_dataset.py --n 5

# Custom sweep count and output path
python scripts/build_synthetic_dataset.py --n 100 --out outputs/datasets/my_dataset.jsonl
```

Expected output size:
- ~80 templates × 50 sweeps = ~4 000 raw examples (before augmentation)
- No API keys required. Pure deterministic parameter sweep.

---

## 2. Augment goals with Gemini paraphrases (optional, one-time)

```bash
export GEMINI_API_KEY=your_key_here
python scripts/augment_goals.py \
    --in outputs/datasets/synthetic_cad_<ts>.jsonl \
    --out outputs/datasets/synthetic_cad_augmented.jsonl \
    --model gemini-2.0-flash \
    --delay 0.1
```

Each original row expands to 4 rows (1 original + 3 paraphrase voices):
- **Terse spec**: `80mm OD, 21mm thick, 4xM8 aluminium flange`
- **Customer request**: `I need a flange that fits an 80mm shaft with 4 bolt holes`
- **Hobbyist**: `Looking to 3D-print a flange for my CNC project, about 80mm across`

Expected augmented size:
- ~80 templates × 50 sweeps × 4 voices = **~16 000 examples**
- Gemini 2.0 Flash cost: approximately $0.01 for 16k goals at current pricing

---

## 3. Target models

| Model | Size | Recommended |
|-------|------|-------------|
| Qwen2.5-Coder-7B-Instruct | 7B | Primary choice |
| DeepSeek-Coder-6.7B-Instruct | 6.7B | Equivalent alternative |
| Qwen2.5-Coder-1.5B | 1.5B | Fastest, for edge deployment |

---

## 4. Fine-tuning with Unsloth (recommended) or Axolotl

### Unsloth (fastest, runs on a single 4090)

```python
from unsloth import FastLanguageModel

model, tokenizer = FastLanguageModel.from_pretrained(
    "Qwen/Qwen2.5-Coder-7B-Instruct",
    max_seq_length=4096,
    load_in_4bit=True,
)
model = FastLanguageModel.get_peft_model(
    model,
    r=16,
    lora_alpha=32,
    target_modules=["q_proj", "v_proj", "k_proj", "o_proj",
                    "gate_proj", "up_proj", "down_proj"],
    lora_dropout=0.05,
    bias="none",
)
```

Training config:
- Epochs: 3
- Learning rate: 2e-4
- Batch size: 4 (grad accum 4 → effective 16)
- Warmup steps: 50
- Scheduler: cosine
- Sequence length: 4096 (covers longest CadQuery templates)

Dataset format (Alpaca/ChatML):
```json
{
  "instruction": "Generate CadQuery Python code for the following part.",
  "input": "<goal string>",
  "output": "<code string>"
}
```

### Axolotl (YAML-based, more configurable)

```yaml
base_model: Qwen/Qwen2.5-Coder-7B-Instruct
model_type: AutoModelForCausalLM
tokenizer_type: AutoTokenizer
load_in_4bit: true

lora_r: 16
lora_alpha: 32
lora_dropout: 0.05
lora_target_linear: true

datasets:
  - path: outputs/datasets/synthetic_cad_augmented.jsonl
    type: alpaca

sequence_len: 4096
micro_batch_size: 4
gradient_accumulation_steps: 4
num_epochs: 3
learning_rate: 0.0002
lr_scheduler: cosine
warmup_steps: 50

output_dir: ./outputs/finetuned/qwen25-coder-cad
```

---

## 5. Expected hardware and cost

| Setup | GPU | Time | Cost |
|-------|-----|------|------|
| RunPod (recommended) | RTX 4090 24GB | ~2 hrs | ~$30 |
| RunPod (budget) | RTX 3090 24GB | ~4 hrs | ~$20 |
| Local (high-end) | RTX 4090 24GB | ~2 hrs | $0 (power) |

Use Unsloth 4-bit QLoRA to keep VRAM under 20GB.

---

## 6. Plugging the fine-tuned model into ARIA-OS

1. Export to GGUF and create an Ollama modelfile:

```bash
# Convert merged weights to GGUF
python llama.cpp/convert_hf_to_gguf.py ./merged_model --outtype q5_k_m

# Create Ollama modelfile
cat > Modelfile <<EOF
FROM ./qwen25-coder-cad.Q5_K_M.gguf
SYSTEM "You are a CadQuery expert. Generate correct, runnable CadQuery Python code."
PARAMETER num_ctx 4096
EOF

ollama create qwen25-coder-cad -f Modelfile
```

2. Register in `aria_os/llm_client.py` — add the model name to the Ollama provider
   routing in the `generate()` or `generate_code()` method:

```python
# In llm_client.py — add to the Ollama code-generation path
_CAD_MODEL = os.environ.get("ARIA_CAD_MODEL", "qwen25-coder-cad")
```

3. Set the env var before running:

```bash
export ARIA_CAD_MODEL=qwen25-coder-cad
python run_aria_os.py "your goal here"
```

---

## 7. What to measure

| Metric | How to measure | Target |
|--------|---------------|--------|
| Template hit rate | % of goals matched by `_find_template_fuzzy` before LLM call | Baseline (unchanged) |
| LLM code success rate | % of LLM-generated scripts that execute without error | > 80% |
| Anthropic call rate | % of complex-CAD goals that still require Anthropic | < 20% (vs ~100% baseline) |
| Avg code length match | Generated code length vs template ground truth | Within 30% |

Run the existing test suite after integration:

```bash
python -m pytest tests/ -q
```

For focused CAD generation regression tests:

```bash
python -m pytest tests/ -q -k "cad or template or generator"
```
