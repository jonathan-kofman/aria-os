# Research agent prompt template — use this as the base for every research spawn

Do NOT delegate research with one-paragraph prompts. Professional engineering
decisions require primary-source evidence, not blog-post paraphrasing.

Every research-agent spawn MUST include:

## 1. The question

State the question with full technical context. Include:
- what we're building (1-2 sentences)
- what we already tried and what failed (so the agent knows where to dig)
- what we'll DO with the answer (so the agent knows what depth is needed)

## 2. Required sources (minimum 3 primary sources)

List the primary sources the agent MUST consult. Primary = one of:
- official documentation (docs.kicad.org, wiki.freecad.org, docs.python.org/3)
- the source code of the tool itself (read the implementation)
- the formal standard (ASME, ISO, IEEE PDFs)
- a peer-reviewed paper
- the project's own GitHub issues / discussions for unresolved edge cases

Blog posts, Stack Overflow answers, and "here's how I did it" tutorials are
SECONDARY. They may be read AFTER the primary sources and only to confirm
or find an example. Never as the sole basis for a claim.

## 3. The "unverified" discipline

The agent's report MUST include an explicit section:

> **What I could not verify**
> - X: primary source was unavailable / paywalled / behind auth; best guess
>   based on community wiki
> - Y: API behavior not documented; inferred from source but not tested

A report with no unverified section is suspect. Every real technical topic
has edges that the documentation doesn't cover.

## 4. Depth minimum

For API reference research: the agent must read at least ONE code example
from the source project's own test suite or demos. Not a user tutorial.

For file-format research: the agent must open and read at least THREE
real files produced by the tool in question. Not just the grammar spec.

For standards research (ASME / ISO): the agent must read the actual
standard PDF if available; if not, read at least two independent summaries
and flag any disagreements between them.

## 5. Output structure

The report must have these sections (in order):

### TL;DR (3 sentences max)
The one thing we most need to do differently after this research.

### Findings (bulleted, with source URLs per bullet)
Every factual claim links to its source. No orphan claims.

### Code / syntax examples (real, copy-pasteable)
At least 2 examples per API surface documented. Each labeled with the
source file it came from.

### What I could not verify
Required section. Be honest.

### Recommended next action for ARIA-OS
One concrete implementation step the orchestrator can take based on this
research. Include the file path in ARIA-OS that will change.

## 6. Hard rules

- Do NOT fabricate API signatures. If you can't find the method signature
  in the source, say so in "unverified" and leave the example commented-out.
- Do NOT cite a blog post as the only source for a claim.
- Do NOT write "this should work" — write "tested working per [source]"
  or flag it as unverified.
- Do NOT exceed 1200 words. Dense + sourced > long + vague.

---

## Template to copy-paste into Agent tool prompts

```
Deep research: <one-sentence question>.

Context: <2-3 sentences about what we're building and what failed so far>

Required primary sources (consult ALL):
1. <primary source 1 URL>
2. <primary source 2 URL>
3. <primary source 3 URL>

Required depth:
- Read at least one real code example from <source project>'s test suite
  or demos.
- Read at least 3 real example files produced by the tool (if file-format
  research).

Follow the RESEARCH_AGENT_TEMPLATE.md output structure:
  TL;DR (3 sentences max)
  Findings (bulleted, each with source URL)
  Code / syntax examples (real, copy-pasteable, labeled with source)
  What I could not verify (REQUIRED section)
  Recommended next action for ARIA-OS (one step + exact file path)

Hard rules:
- No fabricated API signatures; unverified gets flagged.
- No blog post as sole source.
- No "should work" — use "tested per [source]" or flag unverified.
- Max 1200 words.
```
