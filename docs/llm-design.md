# Self Search LLM Layer Design (Phase 5)

This document specifies the design, provider abstraction, structured prompting, JSON schema enforcement, retry mechanics, and hallucination-free fallback strategy for the Self Search Local LLM Layer.

---

## 1. Principles and Constraints

1. **Zero Paid APIs**: The system uses only open-weight, locally executed models (e.g., Mistral 7B Instruct or Llama 3 via Ollama). No external cloud API keys or proprietary endpoints are permitted.
2. **LLM as Language Layer**: The LLM is **never** the database, search engine, or source of factual truth. It synthesises and explains only what is present in retrieved product records and review evidence.
3. **Evidence Grounding**: Every factual claim in product recommendations must map to retrieved evidence citation IDs (e.g. `["R1", "R2"]`).
4. **Strict JSON Schema**: Model output must strictly validate against Pydantic models. Malformed JSON triggers an automated self-correction retry loop.
5. **No Hallucinations / Safe Fallback**: When evidence is missing or the model cannot generate valid JSON, the system never invents facts, reviews, prices, or ratings. Instead, it returns an explicit `insufficient_evidence: true` or a deterministic safe fallback.

---

## 2. Architecture Overview

```
+-----------------------------------------------------------------------------------------+
|                                    LLM PIPELINE                                         |
|                                                                                         |
|  User Query + Parsed Constraints + Retrieved Products + Evidence Pack ([R1], [R2]...)    |
|                                         |                                               |
|                                         v                                               |
|                         [ PromptBuilder.build_chat_messages ]                           |
|                                         |                                               |
|                                         v                                               |
|                         [ LLMProvider / OllamaProvider ]                                |
|                               (HTTP POST /api/chat)                                     |
|                                         |                                               |
|                                         v                                               |
|                            [ Structured JSON Parsing ]                                  |
|                                         |                                               |
|                 +-----------------------+-----------------------+                       |
|                 |                                               |                       |
|         (Valid JSON Schema)                             (Malformed / Invalid)           |
|                 |                                               |                       |
|                 v                                               v                       |
|          StructuredAnswer                          Retry (up to max_retries=2)          |
|                                                                 |                       |
|                                                       (Exhausted / Offline)             |
|                                                                 |                       |
|                                                                 v                       |
|                                                       Deterministic Safe Fallback       |
+-----------------------------------------------------------------------------------------+
```

---

## 3. Pydantic Response Schema

All generation adheres strictly to the following contract:

```python
class EvidenceClaim(BaseModel):
    claim: str
    evidence_ids: list[str] = Field(default_factory=list)

class ProductRecommendation(BaseModel):
    product_id: int
    external_id: Optional[str] = None
    title: Optional[str] = None
    why_it_matches: list[EvidenceClaim] = Field(default_factory=list)
    watch_outs: list[EvidenceClaim] = Field(default_factory=list)

class StructuredAnswer(BaseModel):
    summary: str
    recommendations: list[ProductRecommendation] = Field(default_factory=list)
    insufficient_evidence: bool = False
    missing_information: Optional[str] = None
```

---

## 4. Provider Abstraction

- **`LLMProvider`**: Abstract interface defining `generate(messages, temperature=0.2, **kwargs) -> LLMResponse`.
- **`OllamaProvider`**: Talks to the local Ollama daemon (`http://localhost:11434`), records request latency in milliseconds, and enforces `format="json"`.
- **`MockLLMProvider`**: Deterministic mock provider for fast, reproducible unit testing without model dependencies.

---

## 5. Retry Loop and Safe Fallback Strategy

1. **Attempt 0**: Initial inference request with structured prompt and JSON enforcement.
2. **Attempt 1..N (Retries)**: If parsing or Pydantic validation fails, the validation error is fed back to the model in a follow-up conversation turn, instructing it to correct its output.
3. **Deterministic Fallback**: If retries are exhausted or if the Ollama daemon is unreachable:
   - `build_safe_fallback()` constructs a valid `StructuredAnswer` directly from the top retrieved product records and review evidence.
   - Verified catalog prices, ratings, and real review snippets are used.
   - No statements beyond retrieved database contents are generated.
   - If candidate products are empty, `insufficient_evidence: true` is returned immediately without model invocation.
4. **Reporting the outcome (Phase 9)**: `generate_answer_with_mode()` returns `(answer, GenerationInfo)`, recorded where the outcome happens:
   - `mode="llm"` (with model and attempt count);
   - `mode="fallback"` with `reason` ∈ `provider_unavailable`, `provider_error`, `invalid_output_after_retries`, `unexpected_error`;
   - `mode="none"` (no products, so no model call).

   `generate_answer()` keeps its signature and returns only the answer. The agent exposes this as `AgentResponse.generation`, so clients never infer it from `/health`.

**Time budget (2026-10-22).** Each chat message gets one model-time budget, `SHELFSENSE_OLLAMA_TIMEOUT`, by default 110 s and below the UI's 120 s request timeout. The budget comes from `src/llm/deadline.py` and is opened by `AgentController.handle_message`.
- LLM query parsing (brand-cue messages only, at most 45 s), the answer and any JSON retry all draw from it. Each Ollama call gets only the time left.
- A retry starts only if at least 30 s remain.
- When the budget runs out, the deterministic fallback above answers. A slow CPU therefore produces a labelled fallback, never a UI error.
- Ollama requests also set `num_predict` (700) and `keep_alive` (30 m).
- The prompt asks for at most 3 recommendations, at most 2 reasons and 1 watch-out each, and `[]` (never `null`) for empty lists. The schema also reads a `null` claim list as empty.

**Observed limitation:** fallback answers quote review titles ("Customer review mentions: '…'"). The Phase 6 validator often rates these low-similarity, so offline answers score low support rates (0.22–0.56 measured). The validator is deliberately not relaxed; see `limitations.md`.

---

## 6. Setup Instructions & Hardware Assumptions

### Local Environment Setup
1. **Install Ollama**:
   - Download and install Ollama from [ollama.com](https://ollama.com).
2. **Pull Supported Model**:
   ```bash
   ollama pull mistral:7b-instruct
   # or
   ollama pull llama3:8b-instruct
   ```
3. **Verify Daemon**:
   ```bash
   curl http://localhost:11434/api/tags
   ```

### Hardware Assumptions
- **Recommended**: 16 GB RAM with Apple Silicon (M1/M2/M3) or NVIDIA GPU (6GB+ VRAM).
- **Minimum**: 8 GB RAM on x86_64 CPU (inference speed ~5–10 tokens/sec with 4-bit quantised 7B/8B models).
- **Offline Readiness**: Once Ollama and model weights are downloaded, all LLM inference runs 100% offline without internet access.
