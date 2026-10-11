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

- Ollama: Pydantic AI's OpenAI-compatible chat model pointed at `{ollama_base_url}/v1` (use the `OllamaProvider` if the installed Pydantic AI version has it, otherwise `OpenAIProvider(base_url=…)`; check the installed version's docs for exact class names)
- Anthropic: Pydantic AI's Anthropic model with the key from settings
- Cached per role

---

### B. `Embedder` protocol + `OllamaEmbedder`

- `embed_documents(texts: list[str]) -> list[list[float]]`
- `embed_query(text: str) -> list[float]`
- Calls Ollama `POST {ollama_base_url}/api/embed` with `{"model": ollama_embed_model, "input": [...], "options": {"num_ctx": 8192}}`
- `nomic-embed-text` needs task prefixes:
    - documents: `"search_document: " + text`
    - queries: `"search_query: " + text`
- Asserts every vector has length `settings.embed_dim` (768)
- Batches of 32; `httpx` with 30 s timeout and 2 retries

---

### C. `get_embedder() -> Embedder`

- Always returns `OllamaEmbedder` (decision: embeddings stay local)

---

### D. `check_ollama_models() -> list[str]`

- `GET /api/tags`; returns the configured models that are **not** pulled
- Called at API startup when `LLM_PROVIDER=ollama`: logs a warning such as `Model qwen2.5:14b not found. Run: ollama pull qwen2.5:14b`

---

### E. `scripts/check_llm.py` (run this in week 1)

- Defines a toy tool `get_weather(city: str)` and asks 5 different prompts that need it
- Prints the tool-call success rate for the configured agent model, plus one embedding round-trip
- **Target: 5/5.** Below that, try a larger model before building the router on it

---

## 6. Changes to `api/main.py`

- On startup: if provider is `ollama`, run `check_ollama_models()` and log missing models

---

## 7. Files to Change

- `api/main.py`, `.env.example`
- `Makefile` → add `check-llm` (`uv run python -m scripts.check_llm`)

---

## 8. Files to Create

- `backend/planner_core/llm.py`
- `backend/scripts/check_llm.py`
- `backend/tests/test_llm.py`

---

## 9. Dependencies

- `pydantic-ai-slim[openai,anthropic]`

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

- Ollama not running → clear error: `Ollama not reachable at http://localhost:11434. Start it with 'ollama serve'.`
- Model not pulled → error naming the `ollama pull` command
- Wrong embedding length → `ValueError` naming the model and expected dim

---

## 14. Definition of Done

- [ ] Provider switch works by env var alone
- [ ] Embedder applies nomic prefixes and checks dimension
- [ ] Startup warns about missing Ollama models
- [ ] `check_llm.py` reports tool-calling reliability
- [ ] `make check-llm` target added
