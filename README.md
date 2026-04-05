track LLM CLI usage windows with ambient feedback: keyboard brightness, voice readouts, and an HTTP /status API for consumers like utop.

darker keyboard = less remaining usage; resets to full brightness when the tracked window resets.

## providers

- `claude`: polls anthropic oauth usage windows (five-hour + weekly windows from `/limits`)
- `codex`: polls codex/chatgpt usage API (`https://chatgpt.com/backend-api/wham/usage`)
- `codex_logs`: reads local codex rollout logs from `~/.codex/sessions` and uses the same `/status` rate-limit windows

configure provider and tracked window in `config.yaml`.

# tasks

## todo

## ongoing

### active


### passive

- account support: `ses_2ac4222c4ffeb3hlQz0LSMF5cs`
- optimization/`utop`: `ses_2aa243ba1ffe7gZUNwAfLlKrL5`

### waiting

- [ ] Codex support via API - `ses_344c1d07cffeBslyiLe6CdRXbA`
    - [x] initial API implementation
    - [ ] monitor for stability

## done

- [x] rename brightness-monitor to llm-usage; migrate setuptools+uv to poetry; fix mac pyobjc propagation in prism-shared-python - `ses_2a4924e52ffeGhOPXjqH008YDR`

## cancelled
