"""Sandbox authorization resolution must stay off the async event loop."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from deerflow.authz import sandbox_authz
from deerflow.config.app_config import AppConfig
from deerflow.config.authorization_config import AuthorizationConfig, AuthorizationProviderConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.sandbox import tools as sandbox_tools

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("tool_name", ["write_file_tool", "str_replace_tool"])
async def test_readonly_file_tools_check_config_off_loop_before_acquire(tmp_path, monkeypatch, tool_name):
    probe = tmp_path / "readonly-config"
    await asyncio.to_thread(probe.write_text, "/mnt/skills", encoding="utf-8")
    event_loop_thread = threading.get_ident()
    config_threads = []
    acquisitions = []

    def load_skills_path():
        config_threads.append(threading.get_ident())
        return probe.read_text(encoding="utf-8")

    async def acquire(runtime):
        acquisitions.append(runtime)
        raise AssertionError("read-only mutation must not acquire a sandbox")

    monkeypatch.setattr(sandbox_tools, "_get_skills_container_path", load_skills_path)
    monkeypatch.setattr(sandbox_tools, "ensure_sandbox_initialized_async", acquire)
    runtime = SimpleNamespace(state={}, context={}, config={})
    kwargs = {"runtime": runtime, "path": "/mnt/skills/public/example/SKILL.md"}
    kwargs.update({"content": "after"} if tool_name == "write_file_tool" else {"old_str": "before", "new_str": "after"})

    result = await getattr(sandbox_tools, tool_name).coroutine(**kwargs)

    assert result.startswith("Error:")
    assert config_threads and all(worker != event_loop_thread for worker in config_threads)
    assert acquisitions == []


async def test_reused_async_sandbox_offloads_config_and_provider_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Config hashing and class discovery stay off-loop; construction does not."""
    probe = tmp_path / "sandbox-authz-probe"
    await asyncio.to_thread(probe.write_text, "probe", encoding="utf-8")

    app_config = AppConfig(
        models=[ModelConfig(name="gpt-4", model="gpt-4", use="langchain_openai:ChatOpenAI")],
        sandbox=SandboxConfig(use="deerflow.sandbox.local:LocalSandboxProvider"),
        authorization=AuthorizationConfig(
            enabled=True,
            fail_closed=True,
            default_role="user",
            provider=AuthorizationProviderConfig(
                use="deerflow.authz.rbac:RbacAuthorizationProvider",
                config={"roles": {"user": {"sandbox": {"allow": "*"}}}},
            ),
        ),
    )

    def blocking_config_load():
        probe.read_text(encoding="utf-8")
        return app_config

    discover_provider = sandbox_authz.resolve_authorization_provider_spec

    def blocking_provider_discovery(config):
        probe.read_text(encoding="utf-8")
        return discover_provider(config)

    monkeypatch.setattr(sandbox_authz, "safe_app_config", blocking_config_load)
    monkeypatch.setattr(sandbox_authz, "resolve_authorization_provider_spec", blocking_provider_discovery)

    sandbox = MagicMock()
    sandbox_provider = MagicMock()
    sandbox_provider.get.return_value = sandbox
    monkeypatch.setattr(sandbox_tools, "get_sandbox_provider", lambda: sandbox_provider)
    runtime = SimpleNamespace(
        state={"sandbox": {"sandbox_id": "sbx-existing"}},
        context={"thread_id": "t1", "user_id": "u1", "user_role": "user"},
        config=None,
    )

    assert await sandbox_tools.ensure_sandbox_initialized_async(runtime) is sandbox
