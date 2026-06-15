# OpenHands V1 — Setup Notes (verified)

Environment: Windows 11 → WSL2 (Ubuntu 24.04.4, kernel 6.6.114, systemd on),
Docker 29.5.3 + Compose v5.1.4, Python 3.12.3, Poetry 2.4.1, RTX 5090 Laptop 24 GB,
Ollama 0.22.0 on `:11434`. This checkout is **OpenHands V1** (`openhands-ai` v1.8.0 on
`openhands-sdk`/`openhands-agent-server`/`openhands-tools` 1.28.0).

All commands below were run and confirmed working on 2026-06-14.

## What's installed

- Source build of this repo (`make build`) — already complete: poetry venv with V1
  deps, `frontend/node_modules` + `frontend/build`, Playwright Chromium, pre-commit hook.
- Standalone V1 CLI via uv: `uv tool install openhands --python 3.12`
  → executables `openhands`, `openhands-acp` in `~/.local/bin` (bundles SDK v1.21.0).
- Headless SDK runner: `scripts/oh_run.py` (uses this repo's SDK 1.28.0).

## Run modes

### 1. Web GUI (interactive, one model per conversation)

```bash
make run                 # backend :3000 + frontend :3001 (defaults)
# open http://localhost:3001 ; set model + key in Settings (Ctrl+, / gear)
```

Settings persist under `~/.openhands`. The web app does **not** read `config.toml`'s
`[llm.*]` or `LLM_*` env vars — configure the model in the UI.

**V1 GUI runs each conversation in a sandbox = the agent-server container.** Pull it once:

```bash
docker pull ghcr.io/openhands/agent-server:1.28.0-python   # ~5.4 GB
```

**Running on non-default ports (3000 is taken here by the Friendly gateway):** the GUI's
internal callbacks default to port 3000. When you move the backend, you MUST redirect both
the MCP callback (`web_url`) and the webhook callback (`host_port`), or conversations fail
(MCP "Session terminated", then webhook 500s, 0 events):

```bash
unset SESSION_API_KEY LLM_MODEL LLM_API_KEY LLM_BASE_URL   # avoid leaking into the container
OPENHANDS_SUPPRESS_BANNER=1 \
OH_WEB_URL=http://host.docker.internal:3100 \              # MCP callback → /mcp/mcp
SANDBOX_HOST_PORT=3100 \                                   # webhook callback → /api/v1/webhooks
  make run BACKEND_HOST=127.0.0.1 BACKEND_PORT=3100 FRONTEND_HOST=127.0.0.1 FRONTEND_PORT=3101
```

Ollama from inside the container: use `http://host.docker.internal:11434` in the LLM profile
(WSL2/Docker Desktop relays it to the host even though Ollama binds 127.0.0.1).
VERIFIED end-to-end: an Opus conversation created the requested file in the sandbox
(`/workspace/project/gui-ok.txt` = `gui-ok`, 6 bytes). Local 27B works too but is slow —
the agent requests a 131K context, so prompt-processing on a 24 GB GPU takes minutes.

`RUNTIME=local` (process sandbox, no Docker) exists but is unreliable here: agent-server
cold start exceeds the 120s `sandbox_startup_timeout`, and the conversation→agent wiring is
flaky. Use the Docker sandbox for the GUI.

### 2. Headless SDK runner (multi-model, scriptable across repos) — PRIMARY

Named profiles live in `config.toml` (git-ignored; copy from `config.toml.example`).
Secrets come from `.env` / the environment via each profile's `api_key_env`.

```bash
# Local inner-loop (Ollama Qwen3.6-27B, free) — VERIFIED
poetry run python scripts/oh_run.py --profile local \
  --repo /path/to/repo --max-iters 12 -t "your task"

# Frontier (Anthropic Opus 4.8) — VERIFIED (needs ANTHROPIC_API_KEY)
poetry run python scripts/oh_run.py --profile opus \
  --repo /path/to/repo -t "your task"

# Other profiles: gemini, kimi-ollama (Ollama Cloud), draft_editor
# Task from a file instead of inline:  -f task.txt
```

Set `OPENHANDS_SUPPRESS_BANNER=1` to quieten the SDK banner.

**Local profile bomb-out (turn 1 OK, exits ~15-30s into turn 2) — FIXED.** The
`odytrice/qwen3.6:4090-27b` Modelfile pins `num_ctx 131072` (128K) + `num_gpu 999`.
On the 24 GB GPU that KV cache OOMs/stalls once multi-turn context grows, killing
the run. Fix: a capped model **`qwen3.6-27b-agentic`** (num_ctx 32768, blob-shared
so no extra disk) is the local model for all three entrypoints. Create/recreate it
(this Ollama 0.22.0 needs a real file, not `-f -`):

```bash
printf 'FROM odytrice/qwen3.6:4090-27b\nPARAMETER num_ctx 32768\n' > /tmp/MF
ollama create qwen3.6-27b-agentic -f /tmp/MF
```

The `[llm.local]` / `[llm.draft_editor]` profiles point at
`ollama_chat/qwen3.6-27b-agentic` and also set `num_ctx = 32768` as a guard
(`oh_run.py` forwards it to Ollama as `options.num_ctx` via the SDK's
`litellm_extra_body`). For the standalone CLI use
`LLM_MODEL=ollama_chat/qwen3.6-27b-agentic`; in the Web GUI select that model in
Settings. Verified 2026-06-15: the full `--profile local --max-iters 12`
exploration task ran 7 turns to a clean exit (peak 23.8K tokens), where it
previously bombed on turn 2.

### 3. Standalone CLI headless (env-override) — VERIFIED

```bash
# Local (free); LLM_API_KEY must be non-empty even for Ollama → use a dummy.
# Use the num_ctx-capped variant (see the local-profile bomb-out note above).
LLM_MODEL=ollama_chat/qwen3.6-27b-agentic \
LLM_BASE_URL=http://localhost:11434 \
LLM_API_KEY=dummy \
openhands --headless --override-with-envs -t "your task"

# Frontier
LLM_MODEL=anthropic/claude-opus-4-8 LLM_API_KEY="$ANTHROPIC_API_KEY" \
openhands --headless --override-with-envs -t "your task"

# Interactive TUI:  openhands         |  Resume:  openhands --resume <id>
# JSONL events for automation:  add --json
```

The CLI's default base URL is the all-hands cloud proxy; `--override-with-envs` makes
`LLM_MODEL`/`LLM_API_KEY`/`LLM_BASE_URL` win. CLI settings persist under `~/.openhands`.

## Smoke-test results (2026-06-14)

| Test | Path | Model | Tool-calling | Outcome |
|------|------|-------|--------------|---------|
| A | SDK runner | `ollama_chat/odytrice/qwen3.6:4090-27b` | native, `file_editor` | hello.txt = `oh-smoke-ok`, $0.00 |
| B | SDK runner | `anthropic/claude-opus-4-8` | native, `terminal`+`xxd` | frontier.txt = `opus-ok`, ~$0.09, 49% cache hit |
| C | standalone CLI | `ollama_chat/odytrice/qwen3.6:4090-27b` | native | cli.txt = `cli-ok` |
| D | Web GUI (Docker sandbox) | `anthropic/claude-opus-4-8` | native (+5 MCP tools) | gui-ok.txt = `gui-ok` (6 bytes) in container `/workspace/project` |

## Where secrets live

- Real keys: `.env` (git-ignored) or exported WSL env vars. `ANTHROPIC_API_KEY` is
  currently set in the WSL environment.
- Tracked templates only: `.env.template`, `config.toml.example`. No real keys are
  ever committed (`.gitignore` already blocks `.env*` and `config.toml`).

## Ollama networking

- Host-side callers (SDK runner, standalone CLI on WSL) → `http://localhost:11434`.
- In-container callers (Docker GUI runtime) → `http://host.docker.internal:11434`
  plus `--add-host=host.docker.internal:host-gateway` (already in docker-compose.yml
  and `config.template.toml` `runtime_extra_build_args`).

## Model slugs (verify newest against https://docs.litellm.ai/docs/providers)

`anthropic/claude-opus-4-8` · `gemini/gemini-3.1-pro-preview` ·
`ollama_chat/qwen3.6-27b-agentic` (num_ctx-capped variant of
`ollama_chat/odytrice/qwen3.6:4090-27b`) · `ollama_chat/kimi-k2.6:cloud`

### Profile → route → role (active)

| Profile | Slug | Route | Key | Role |
|---|---|---|---|---|
| `opus` | `anthropic/claude-opus-4-8` | Anthropic direct | `ANTHROPIC_API_KEY` | Frontier driver |
| `gemini` | `gemini/gemini-3.1-pro-preview` | Google AI Studio | `GEMINI_API_KEY` | Frontier / multimodal |
| `local` | `ollama_chat/qwen3.6-27b-agentic` | Ollama local GPU | — | Inner-loop (num_ctx 32768) |
| `kimi-ollama` | `ollama_chat/kimi-k2.6:cloud` | Ollama Cloud (via :11434) | `ollama signin` | Open-weight gateway |
| `draft_editor` | `ollama_chat/qwen3.6-27b-agentic` | Ollama local GPU | — | Cheap diff drafting (num_ctx 32768) |

No OpenRouter dependency: the `kimi` (OpenRouter) and `deepseek` profiles are commented
out in `config.toml.example`, and `OPENROUTER_API_KEY` is commented out in `.env*`.
Re-enable by uncommenting both. Routing is manual (pick `--profile`); `[model_routing]`
is `noop_router` (no auto-switch).

Available in Ollama but intentionally unallocated: `minimax-m3:cloud`, `gemma4:31b`,
`glm-4.7-flash:latest`, and the retired `qwen3.5:35b-a3b-q4_K_M`.
