#!/usr/bin/env python3
"""Headless OpenHands V1 runner — multi-model, multi-repo.

OpenHands V1 dropped the V0 ``python -m openhands.core.main --llm-config <name>``
headless entrypoint. This runner restores that ergonomics on top of the V1
``openhands-sdk``: it reads named ``[llm.<profile>]`` blocks from a TOML file and
drives a one-shot, auto-approving agent run against a chosen repository.

Secrets never live in the TOML. Each profile names an environment variable via
``api_key_env``; the key is resolved from the process environment (optionally
seeded from a local ``.env`` via python-dotenv).

Examples
--------
    poetry run python scripts/oh_run.py --profile local \
        --repo /home/sean/openclaw -t "List the top-level modules."

    poetry run python scripts/oh_run.py --profile opus \
        --repo /home/sean/lwm2m-cov -f task.txt --max-iters 200
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from pathlib import Path

from dotenv import load_dotenv
from pydantic import SecretStr

from openhands.sdk import LLM, Conversation
from openhands.tools.preset.default import get_default_agent


def _load_profiles(path: Path) -> dict:
    """Parse the TOML profile file and return its ``[llm]`` table."""
    if not path.is_file():
        sys.exit(f'error: profile file not found: {path}')
    with path.open('rb') as handle:
        data = tomllib.load(handle)
    profiles = data.get('llm')
    if not isinstance(profiles, dict) or not profiles:
        sys.exit(f'error: no [llm.<name>] profiles found in {path}')
    return profiles


def _build_llm(name: str, profile: dict) -> LLM:
    """Construct an SDK ``LLM`` from a single profile block."""
    model = profile.get('model')
    if not model:
        sys.exit(f"error: profile [llm.{name}] is missing 'model'")

    api_key: SecretStr | None = None
    key_env = profile.get('api_key_env')
    if key_env:
        raw = os.environ.get(key_env)
        if not raw:
            sys.exit(
                f'error: profile [llm.{name}] needs env var {key_env}, '
                f'which is unset. Add it to .env or export it.'
            )
        api_key = SecretStr(raw)

    kwargs: dict = dict(
        model=model,
        api_key=api_key,
        base_url=profile.get('base_url'),
        usage_id='agent',
        native_tool_calling=profile.get('native_tool_calling', True),
    )

    # Optional Ollama context-window cap. The local model's Modelfile may pin a
    # very large num_ctx (e.g. 131072); allocating that KV cache OOMs/stalls a
    # constrained GPU on multi-turn runs. Forwarding a smaller num_ctx as a
    # litellm option keeps agentic turns (~8-15K tokens) within VRAM.
    num_ctx = profile.get('num_ctx')
    if num_ctx is not None:
        kwargs['litellm_extra_body'] = {'num_ctx': int(num_ctx)}

    return LLM(**kwargs)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Headless OpenHands V1 runner (SDK-based, multi-model).'
    )
    parser.add_argument(
        '--profile', required=True, help='Name of a [llm.<name>] block in the TOML.'
    )
    parser.add_argument(
        '--repo', default='.', help='Repository/workspace path (default: cwd).'
    )
    parser.add_argument(
        '--profiles',
        default='config.toml',
        help='TOML file holding [llm.<name>] profiles (default: config.toml).',
    )
    parser.add_argument(
        '--max-iters',
        type=int,
        default=100,
        help='Maximum agent iterations per run (default: 100).',
    )
    parser.add_argument(
        '--env-file',
        default='.env',
        help='dotenv file to seed secrets from (default: .env).',
    )
    task = parser.add_mutually_exclusive_group(required=True)
    task.add_argument('-t', '--task', help='Inline task text.')
    task.add_argument(
        '-f', '--file', help='Path to a file whose contents are the task.'
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    env_file = Path(args.env_file)
    if env_file.is_file():
        load_dotenv(env_file)

    profiles = _load_profiles(Path(args.profiles))
    if args.profile not in profiles:
        sys.exit(
            f"error: profile '{args.profile}' not in {args.profiles}. "
            f'Available: {", ".join(sorted(profiles))}'
        )

    if args.file:
        task_text = Path(args.file).read_text(encoding='utf-8').strip()
    else:
        task_text = args.task

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        sys.exit(f'error: repo path is not a directory: {repo}')

    llm = _build_llm(args.profile, profiles[args.profile])

    print(
        f'[oh_run] profile={args.profile} model={llm.model} '
        f'base_url={llm.base_url or "default"} repo={repo}',
        flush=True,
    )

    agent = get_default_agent(llm=llm, cli_mode=True)
    conversation = Conversation(
        agent=agent,
        workspace=str(repo),
        max_iteration_per_run=args.max_iters,
    )
    conversation.send_message(task_text)
    conversation.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
