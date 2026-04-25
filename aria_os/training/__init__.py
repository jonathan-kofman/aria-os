"""Training data export — converts accepted feedback into JSONL
ready for LLM SFT / preference fine-tuning.

We don't run training here; we just produce clean, deduped data
in the formats Anthropic + OpenAI + Together accept.

Outputs:
    outputs/training/aria_sft_anthropic_<ts>.jsonl
    outputs/training/aria_sft_openai_<ts>.jsonl
    outputs/training/aria_dpo_<ts>.jsonl  (when reject pairs exist)

Anthropic format (messages):
    {"messages": [{"role":"system","content":...},
                  {"role":"user","content":...},
                  {"role":"assistant","content": <plan_json>}]}

OpenAI format (chat-completions):
    {"messages": [{"role":"system",...},{"role":"user",...},
                  {"role":"assistant","content": <plan_json>}]}

DPO format (paired preference data):
    {"prompt": <user_msg>,
     "chosen":   <accepted_plan_json>,
     "rejected": <rejected_plan_json>}
"""
from .export_sft import export_sft, export_dpo

__all__ = ["export_sft", "export_dpo"]
