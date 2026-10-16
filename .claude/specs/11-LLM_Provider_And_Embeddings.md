## 1. Overview

One place that decides **which LLM and which embedding model** the app uses, switched by a single environment variable.

- **Default (build and personal use):** Ollama on your machine — `qwen2.5:14b` for the agent, `nomic-embed-text` for embeddings. Cost: €0.
- **Demo:** set `LLM_PROVIDER=anthropic` to use Claude for the agent. Embeddings **stay local** in every mode.

Nothing else in the router, tools or tests changes when you flip the variable.

---

## 2. Depends on

- 01-Project_Setup

---

## 3. Routes

- No new routes (`/health` already reports Ollama)

---

## 4. Database Schema

- No changes

---

## 5. Functions to Implement (`planner_core/llm.py`)

---

### A. `get_model(role: Literal["agent", "router"]) -> pydantic_ai.models.Model`

| `LLM_PROVIDER` | `role = agent` | `role = router` |
| --- | --- | --- |
| `ollama` | `ollama_chat_model` (`qwen2.5:14b`) | `ollama_router_model` |
| `anthropic` | `anthropic_model` (Sonnet) | `anthropic_router_model` (Haiku) |

- Ollama: `OllamaModel(name, provider=OllamaProvider(base_url=f"{ollama_base_url}/v1"))` — resolved against the installed Pydantic AI **v2.56** (`pydantic_ai.models.ollama`), which has a dedicated `OllamaModel` subclassing `OpenAIChatModel`. The `/v1` suffix is required: it is Ollama's OpenAI-compatible surface, while `/api` is its own REST API (what the embedder and `/api/tags` use)
- Anthropic: `AnthropicModel(name, provider=AnthropicProvider(api_key=…))` with the key from settings
- Cached per role, by hand rather than with `functools.cache`, so `reset_models()` can clear it alongside the settings cache in tests

---

### B. `Embedder` protocol + `OllamaEmbedder`

- `embed_documents(texts: list[str]) -> list[list[float]]`
- `embed_query(text: str) -> list[float]`
- Calls Ollama `POST {ollama_base_url}/api/embed` with `{"model": ollama_embed_model, "input": [...], "options": {"num_ctx": 8192}}`
- `num_ctx` is sent explicitly because Ollama's own default for this model is 2048 and `truncate` defaults to true, so a long note would otherwise embed as its first half with no error
- `nomic-embed-text` needs task prefixes:
    - documents: `"search_document: " + text`
    - queries: `"search_query: " + text`
- Parses the response body as a Pydantic model (`EmbedResponse.embeddings: list[list[float]]`) **and then** asserts every vector has length `settings.embed_dim` (768). The length assertion alone is not sufficient and was the spec's original wording: `[[None] * 768]` and a 768-character string both have the right length, and both would be stored as "vectors"
- Batches of 32; `httpx` with 30 s timeout and 2 retries. `/api/tags` uses its own shorter 5 s timeout, because it is probed at startup
- Validates its own construction: `retries >= 0`, `dim >= 1`

---

### C. `get_embedder() -> Embedder`

- Always returns `OllamaEmbedder` (decision: embeddings stay local)

---

### D. `check_ollama_models(client=None) -> list[str]`

- `GET /api/tags`; returns the configured models that are **not** pulled
- Tag names are normalised before comparison: Ollama always reports an explicit tag, so a setting written as `nomic-embed-text` has to be matched against `nomic-embed-text:latest` or a pulled model reads as missing
- Raises `OllamaUnreachable` when Ollama cannot be asked at all — distinct from a missing model, about which nothing can then be concluded
- `warn_missing_models()` wraps it for startup when `LLM_PROVIDER=ollama`: logs a warning such as `Model qwen2.5:14b not found. Run: ollama pull qwen2.5:14b`, and never raises

---

### E. `scripts/check_llm.py` (run this in week 1)

- Defines a toy tool `get_weather(city: str)` and asks 5 different prompts that need it
- Prints the tool-call success rate for the configured agent model, plus one embedding round-trip
- **Target: 5/5.** Below that, try a larger model before building the router on it
- Checks `check_ollama_models()` **first** and exits 2 if anything is unpulled: measuring anyway would score a missing model 0/5 and send the reader to install a bigger one to fix a download that never happened
- Exit codes: 0 all passed, 1 below target or a dimension mismatch, 2 nothing could be measured

---

## 6. Changes to `api/main.py`

- On startup: if provider is `ollama`, run `check_ollama_models()` and log missing models

---

## 7. Files to Change

- `api/main.py` (lifespan + the 503 handler), `planner_core/errors.py` (`LlmUnavailable`, `OllamaUnreachable`, `ModelNotPulled`), `README.md`
- `Makefile` → add `check-llm` (`uv run python -m scripts.check_llm`)
- **Not** `.env.example`: it is gitignored (`.gitignore` line 3) so it cannot be committed, and spec 11 adds no new settings — every field it needs already exists in `config.py` from spec 01

---

## 8. Files to Create

- `backend/planner_core/llm.py`
- `backend/scripts/check_llm.py`
- `backend/tests/test_llm.py`
- `backend/tests/fakes.py` — the deterministic `FakeEmbedder` spec 12's retrieval tests need; it belongs with the embedder it stands in for

---

## 9. Dependencies

- `pydantic-ai-slim[openai,anthropic]>=2` (installed: 2.56.0, which also brings `httpx2` alongside the existing `httpx`)

---

## 10. Model Choices

| Model (Ollama) | Approx. size | Use |
| --- | --- | --- |
| `qwen2.5:14b` | ~9 GB | Default agent + router |
| `qwen2.5:32b` | ~20 GB | If your machine has the memory; most reliable tool calling |
| `mistral-nemo:12b` | ~7 GB | Fallback when memory is tight |
| `nomic-embed-text` | ~0.3 GB | Embeddings, 768 dims, 8k context |

- Stay at 14B or above for the agent; small models are flaky with structured tool calls
- Check `ollama ps` after loading: if the model spills to CPU it will be slow

---

## 11. Rules for Implementation

- No module other than `planner_core/llm.py` imports a provider SDK
- Tests use Pydantic AI's `TestModel` / `FunctionModel`, never a real LLM
- Never log API keys; `SecretStr` only
- Changing `ollama_embed_model` requires matching `embed_dim` and a re-embed (document this in the README)

---

## 12. Expected Behavior

- `LLM_PROVIDER=ollama` → agent runs against local Qwen; no network calls leave the machine except Langfuse traces
- `LLM_PROVIDER=anthropic` → same code path, Claude models
- `embed_query("knee pain")` returns 768 floats
- `make check-llm` prints success rate and embedding dimension

---

## 13. Error Handling Expectations

- Ollama not running → `OllamaUnreachable`: `Ollama not reachable at http://localhost:11434. Start it with 'ollama serve'.`
- Model not pulled → `ModelNotPulled`, naming the `ollama pull` command
- Wrong embedding length → `ValueError` naming the model and expected dim
- Both exceptions share a `LlmUnavailable` base so spec 12 can keep a training log with one `except`, and the API maps that base to **503** — the request was well formed and the machine behind it was not
- A 5xx or a dropped connection is retried twice; a 404 is not, because a model does not download while we wait

---

## 14. Definition of Done

- [ ] Provider switch works by env var alone
- [ ] Embedder applies nomic prefixes and checks dimension
- [ ] Startup warns about missing Ollama models
- [ ] `check_llm.py` reports tool-calling reliability
- [ ] `make check-llm` target added
