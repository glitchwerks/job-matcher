# TODO

## CI/CD: GitHub Actions (issue #19)

- [x] Create `requirements-dev.txt` with `pytest` and `ruff`
- [x] Create `.github/workflows/ci.yml` (trigger on push/PR to `main`)
- [x] Configure Python 3.11 environment in workflow
- [x] Add `pytest` step (full test suite)
- [x] Add `ruff check .` linting step
- [ ] Verify workflow passes on current codebase

## Feature: Pluggable model provider (issue #8)

- [x] Define `LLMProvider` abstract base class / Protocol in a new `providers/` package (or `scoring.py`)
- [x] Implement `AnthropicProvider` wrapping existing Anthropic SDK logic
- [x] Implement `OpenAIProvider` for GPT-4o-mini / GPT-4o
- [x] Implement `GeminiProvider` for gemini-1.5-flash / gemini-1.5-pro
- [x] Refactor `score_listing()` to accept an `LLMProvider` instead of a raw Anthropic client
- [x] Replace hardcoded Haiku pricing in `db.py` with a per-model pricing table
- [x] Update `get_usage_stats()` to use dynamic pricing (or store provider/model per listing)
- [x] Update cost calculation in `ingest.py` `run()` and `rescore()` to use dynamic pricing
- [x] Add `provider` key to `config.json` `scoring` block (default: `"anthropic"`)
- [x] Add `OPENAI_API_KEY` and `GOOGLE_API_KEY` to `config.example.json` and `.env.example`
- [x] Update `requirements.txt` with `openai` and `google-generativeai` (or `google-genai`)
- [x] Write tests for each provider adapter
