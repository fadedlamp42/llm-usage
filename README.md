sync keyboard brightness to ai usage windows

darker = less remaining usage; reset to full brightness when the tracked window resets.

## providers

- `claude`: polls anthropic oauth usage windows (five-hour + weekly windows from `/limits`)
- `codex`: reads local codex rollout logs from `~/.codex/sessions` and uses the same `/status` rate-limit windows

configure provider and tracked window in `config.yaml`.

# tasks

## todo

## ongoing

### active

- [ ] Codex support via API - `ses_344c1d07cffeBslyiLe6CdRXbA`

### passive

### waiting

## done

## cancelled
