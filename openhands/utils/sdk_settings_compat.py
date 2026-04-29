"""SDK compatibility shim for the discriminated-union ``AgentSettings`` rework."""

from typing import Any

try:
    from openhands.sdk.settings import (  # type: ignore[attr-defined]
        ACPAgentSettings,
        AgentSettingsConfig,
        default_agent_settings,
        export_agent_settings_schema,
        validate_agent_settings,
    )

    try:
        from openhands.sdk.settings import (  # type: ignore[attr-defined]
            OpenHandsAgentSettings,
        )
    except ImportError:
        from openhands.sdk.settings import (  # type: ignore[attr-defined, misc, assignment, no-redef]
            LLMAgentSettings as OpenHandsAgentSettings,
        )

    LLMAgentSettings = OpenHandsAgentSettings
    _HAS_DISCRIMINATED_UNION = True
except ImportError:
    _HAS_DISCRIMINATED_UNION = False
    from openhands.sdk.settings import AgentSettings

    OpenHandsAgentSettings = AgentSettings  # type: ignore[misc, assignment]
    LLMAgentSettings = AgentSettings  # type: ignore[misc, assignment]

    class _ACPAgentSettingsStub:
        """Sentinel — older SDK builds cannot produce ACPAgentSettings instances."""

    ACPAgentSettings = _ACPAgentSettingsStub  # type: ignore[misc, assignment]
    AgentSettingsConfig = AgentSettings  # type: ignore[misc, assignment]

    def default_agent_settings() -> AgentSettings:  # type: ignore[misc]
        return AgentSettings()

    def validate_agent_settings(data: dict[str, Any]) -> AgentSettings:  # type: ignore[misc]
        if isinstance(data, dict) and data.get('kind') == 'acp':
            raise RuntimeError(
                "Stored settings contain kind='acp' but the installed "
                'openhands-sdk does not support ACP agents. Upgrade to an '
                'SDK release that includes the discriminated-union rework '
                '(OpenHands/software-agent-sdk#2861).'
            )
        return AgentSettings.model_validate(data)

    def export_agent_settings_schema():  # type: ignore[misc]
        return AgentSettings.export_schema()


__all__ = [
    'ACPAgentSettings',
    'AgentSettingsConfig',
    'LLMAgentSettings',
    'OpenHandsAgentSettings',
    '_HAS_DISCRIMINATED_UNION',
    'default_agent_settings',
    'export_agent_settings_schema',
    'validate_agent_settings',
]
