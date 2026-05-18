from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializationInfo,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from openhands.app_server.utils.logger import openhands_logger as logger
from openhands.sdk.llm import LLM
from openhands.sdk.settings.model import ACPServerKind


def has_real_api_key(api_key: Any) -> bool:
    """Return True iff ``api_key`` carries a non-empty value.

    A ``SecretStr('')`` should report as *not set* — otherwise the UI tells
    the user a key is stored when it isn't. Mirrors the check used in
    ``Settings.llm_api_key_is_set``.
    """
    if api_key is None:
        return False
    secret_value = (
        api_key.get_secret_value() if isinstance(api_key, SecretStr) else str(api_key)
    )
    return bool(secret_value and secret_value.strip())


# Soft cap — keeps Settings payload bounded and blocks per-user storage
# blow-ups. Tune if product requirements change.
MAX_PROFILES_PER_USER: Final[int] = 10


class ProfileNotFoundError(LookupError):
    """Raised when a profile lookup or activation references an unknown name."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Profile '{name}' not found")


class ProfileLimitExceededError(ValueError):
    """Raised when saving a new profile would exceed :data:`MAX_PROFILES_PER_USER`."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(
            f'Profile limit reached ({limit}). Delete a profile before saving a new one.'
        )


class ProfileAlreadyExistsError(ValueError):
    """Raised when a rename target collides with an existing profile."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Profile '{name}' already exists")


class AgentProfile(BaseModel):
    """A saved agent configuration — either an OpenHands LLM agent or an ACP agent.

    ``extra='ignore'`` makes this backward-compatible with the legacy stored
    format where profiles were serialised as full ``LLM`` model dumps.  Fields
    that exist on ``LLM`` but not here (e.g. ``temperature``, ``num_retries``)
    are silently dropped on load; the three identity fields that matter
    (``model``, ``api_key``, ``base_url``) map directly.

    Invariants:
    - When ``agent_kind='acp'``, ``acp_server`` must be set.
    - When ``agent_kind='openhands'``, ``acp_server`` and ``acp_model`` are ``None``.
    """

    model_config = ConfigDict(extra='ignore')

    # Discriminator — absent in legacy LLM-shaped data → defaults to "openhands".
    agent_kind: Literal['openhands', 'acp'] = 'openhands'

    # Shared credential fields.  For OpenHands agents these go directly into
    # the LLM; for ACP agents they are translated to provider-specific env vars
    # via ACPAgentSettings.resolve_provider_env().
    api_key: SecretStr | None = None
    base_url: str | None = None

    # OpenHands-specific: the LLM model identifier.
    model: str = ''

    # ACP-specific fields.
    acp_server: ACPServerKind | None = None
    acp_model: str | None = None

    @model_validator(mode='after')
    def _validate_acp_fields(self) -> AgentProfile:
        if self.agent_kind == 'acp' and self.acp_server is None:
            raise ValueError("acp_server is required when agent_kind='acp'")
        if self.agent_kind == 'openhands' and (
            self.acp_server is not None or self.acp_model is not None
        ):
            raise ValueError(
                "acp_server and acp_model must be None when agent_kind='openhands'"
            )
        return self

    @field_serializer('api_key', when_used='always')
    def _serialize_api_key(self, value: SecretStr | None, info: SerializationInfo) -> Any:
        if value is None:
            return None
        context = info.context or {}
        expose = context.get('expose_secrets', False)
        if expose == 'plaintext' or expose is True:
            return value.get_secret_value()
        if expose == 'encrypted':
            cipher = context.get('cipher')
            if cipher is not None:
                return cipher.encrypt(value.get_secret_value())
        # Default: mask (same convention as LLM serialization)
        return str(value)

    @classmethod
    def from_llm(cls, llm: LLM) -> AgentProfile:
        """Build an OpenHands profile from a live LLM instance."""
        return cls(
            agent_kind='openhands',
            model=llm.model,
            api_key=llm.api_key,
            base_url=llm.base_url,
        )

    @classmethod
    def from_acp_settings(cls, settings: Any) -> AgentProfile:
        """Build an ACP profile from a live ACPAgentSettings instance."""
        return cls(
            agent_kind='acp',
            acp_server=settings.acp_server,
            acp_model=settings.acp_model,
            api_key=settings.llm.api_key,
            base_url=settings.llm.base_url,
        )



class StrictLLM(LLM):
    """LLM variant that rejects unknown fields.

    The base ``LLM`` model has ``extra='ignore'``, so typos and renamed keys
    silently disappear. For API input we want to fail loud, otherwise users
    can POST ``{"llm": {"custom_header": "x"}}`` and get a 201 with the
    field quietly dropped.
    """

    model_config = ConfigDict(extra='forbid')


class StrictAgentProfile(AgentProfile):
    """AgentProfile variant that rejects unknown fields for API input validation."""

    model_config = ConfigDict(extra='forbid')


class LLMProfiles(BaseModel):
    """Container for saved agent configurations.

    Stores a named collection of :class:`AgentProfile` instances (OpenHands
    LLM configs or ACP configs) plus the name of the currently active one.
    All profile-management logic lives here; ``Settings`` holds a single
    ``LLMProfiles`` instance and delegates to it.

    Invariants (enforced on validate + assignment):
    - ``active`` is either ``None`` or a key of ``profiles``.
    - Individual profiles that fail to parse (schema drift) are dropped with
      a warning rather than failing the whole ``Settings`` load.

    Backward compatibility: profiles stored before this change were serialised
    as full ``LLM`` model dumps.  :class:`AgentProfile` uses ``extra='ignore'``
    and defaults ``agent_kind`` to ``'openhands'``, so old payloads load
    unchanged — extra LLM-only fields are silently dropped.
    """

    model_config = ConfigDict(validate_assignment=True)

    profiles: dict[str, AgentProfile] = Field(default_factory=dict)
    active: str | None = None

    # ── Validation ─────────────────────────────────────────────────

    @field_validator('profiles', mode='before')
    @classmethod
    def _skip_invalid_profiles(cls, value: Any) -> Any:
        """Best-effort per-profile load: skip entries that fail to validate.

        Guards against schema drift — if a single stored profile becomes
        invalid after an upgrade, the user's other profiles and the rest of
        their settings still load.
        """
        if not isinstance(value, dict):
            return value
        valid: dict[str, Any] = {}
        for name, raw in value.items():
            if isinstance(raw, AgentProfile):
                valid[name] = raw
                continue
            try:
                valid[name] = AgentProfile.model_validate(raw)
            except ValidationError as exc:
                logger.warning('Skipping invalid agent profile %r: %s', name, exc)
        return valid

    @model_validator(mode='after')
    def _reconcile_active(self) -> LLMProfiles:
        if self.active is not None and self.active not in self.profiles:
            # Bypass validate_assignment to avoid re-entering this validator.
            object.__setattr__(self, 'active', None)
        return self

    # ── Queries ────────────────────────────────────────────────────

    def get(self, name: str) -> AgentProfile | None:
        """Return the named profile or ``None`` if it doesn't exist."""
        return self.profiles.get(name)

    def require(self, name: str) -> AgentProfile:
        """Return the named profile or raise :class:`ProfileNotFoundError`."""
        profile = self.profiles.get(name)
        if profile is None:
            raise ProfileNotFoundError(name)
        return profile

    def has(self, name: str) -> bool:
        return name in self.profiles

    def summaries(self) -> list[dict[str, Any]]:
        """Return a summary dict per profile for the list endpoint.

        ``api_key_set`` mirrors the ``llm_api_key_set`` convention the main
        settings endpoint already uses, so the frontend can render
        "key stored" vs. "needs key" without fetching each profile.
        """
        return [
            {
                'name': name,
                'agent_kind': p.agent_kind,
                'model': p.model or None,
                'acp_server': p.acp_server,
                'acp_model': p.acp_model,
                'base_url': p.base_url,
                'api_key_set': has_real_api_key(p.api_key),
            }
            for name, p in self.profiles.items()
        ]

    # ── Mutations ──────────────────────────────────────────────────

    def save(
        self,
        name: str,
        profile: AgentProfile | LLM,
        include_secrets: bool = True,
    ) -> None:
        """Save ``profile`` under ``name``. Overwrites if the name exists.

        Accepts either an :class:`AgentProfile` or a legacy :class:`LLM`
        instance (auto-converted to an OpenHands profile) for backward
        compatibility with existing callers.

        Always stores a copy so later caller-side mutations do not bleed into
        the stored profile. Raises :class:`ProfileLimitExceededError` if
        saving a *new* profile would push the count past
        :data:`MAX_PROFILES_PER_USER`.
        """
        if isinstance(profile, LLM):
            profile = AgentProfile.from_llm(profile)

        if name not in self.profiles and len(self.profiles) >= MAX_PROFILES_PER_USER:
            raise ProfileLimitExceededError(MAX_PROFILES_PER_USER)

        update = {} if include_secrets else {'api_key': None}
        self.profiles[name] = profile.model_copy(update=update)

    def rename(self, old_name: str, new_name: str) -> None:
        """Rename a profile, preserving config, insertion order, and the active flag.

        Raises :class:`ProfileNotFoundError` if ``old_name`` doesn't exist,
        or :class:`ProfileAlreadyExistsError` if ``new_name`` is already taken
        by a different profile.
        """
        if old_name not in self.profiles:
            raise ProfileNotFoundError(old_name)
        if new_name == old_name:
            return
        if new_name in self.profiles:
            raise ProfileAlreadyExistsError(new_name)

        # Capture the active name *before* reassigning ``profiles`` — the
        # model_validator runs on assignment and would null out ``active``
        # (old_name no longer exists in the rebuilt dict), so we'd lose the
        # signal otherwise.
        was_active = self.active == old_name

        # Rebuild to preserve insertion order — the renamed profile keeps
        # the slot of the old one rather than moving to the end.
        renamed: dict[str, AgentProfile] = {
            (new_name if key == old_name else key): p
            for key, p in self.profiles.items()
        }
        self.profiles = renamed
        if was_active:
            # Bypass validate_assignment since we know the invariant holds
            # (new_name is now a key of self.profiles).
            object.__setattr__(self, 'active', new_name)

    def delete(self, name: str) -> bool:
        """Delete a profile. Returns True if the profile existed.

        Clears ``active`` if the deleted profile was active.
        """
        if name not in self.profiles:
            return False
        del self.profiles[name]
        if self.active == name:
            # Bypass validate_assignment since we already know the invariant holds.
            object.__setattr__(self, 'active', None)
        return True

    # ── Serialization ──────────────────────────────────────────────

    @field_serializer('profiles')
    def _profiles_serializer(
        self,
        profiles: dict[str, AgentProfile],
        info: SerializationInfo,
    ) -> dict[str, Any]:
        return {
            name: p.model_dump(mode='json', context=info.context)
            for name, p in profiles.items()
        }
