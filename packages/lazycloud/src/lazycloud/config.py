from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.paths import state_home

from lazycloud.json_contracts import validate_json_object

DEFAULT_PROFILE = "default"
DEFAULT_WORKSPACE = "default"

# Placeholder hosted control-plane endpoint shipped in the published `lazycloud`
# dist. It is the final fallback in the endpoint resolution order (flag > env >
# stored profile > this default), so plain `lazycloud login` targets the hosted
# platform with no configuration. Swapped for the real hosted domain at first
# publish.
PACKAGED_DEFAULT_ENDPOINT = "https://api.lazycloud.dev"


class ConfigError(RuntimeError):
    pass


class ClientProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = DEFAULT_PROFILE
    endpoint: str = ""
    workspace: str = DEFAULT_WORKSPACE
    token: str = ""
    tls: bool = False

    def resolved_endpoint(self) -> str:
        """The profile's endpoint, falling back to the packaged default.

        A blank profile endpoint is not an error: the published dist always has
        a hosted default to target, so this always yields a usable endpoint.
        """
        return self.endpoint.strip() or PACKAGED_DEFAULT_ENDPOINT


class ClientConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    active_profile: str = DEFAULT_PROFILE
    profiles: dict[str, ClientProfile] = Field(
        default_factory=lambda: {DEFAULT_PROFILE: ClientProfile()}
    )


class ClientSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_",
        env_file=".env",
        extra="ignore",
    )

    home: Path = Field(default_factory=state_home)
    config: Path | None = None
    profile: str = ""
    endpoint: str = ""
    workspace: str = ""
    token: str = ""
    tls: bool | None = None

    @property
    def home_path(self) -> Path:
        return self.home.expanduser().resolve()

    @property
    def config_path(self) -> Path:
        if self.config is not None:
            return self.config.expanduser().resolve()
        return self.home_path / "config.yaml"


@lru_cache(maxsize=1)
def settings() -> ClientSettings:
    return ClientSettings()


def reset_settings_cache() -> None:
    settings.cache_clear()


def empty_config() -> dict[str, JsonValue]:
    return _config_data(ClientConfig())


def load_config(*, replace_legacy: bool = False) -> dict[str, JsonValue]:
    return _config_data(_load_client_config(replace_legacy=replace_legacy))


def save_config(config: ClientConfig | Mapping[str, JsonValue]) -> None:
    normalized = _normalize_config(config)
    path = settings().config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(_config_data(normalized), handle, sort_keys=True)


def list_profiles() -> list[ClientProfile]:
    return list(_load_client_config().profiles.values())


def active_profile_name() -> str:
    configured = settings().profile.strip()
    if configured:
        return configured
    return _load_client_config().active_profile


def get_profile(name: str | None = None, *, apply_env: bool = True) -> ClientProfile:
    config = _load_client_config()
    selected = name or settings().profile.strip() or config.active_profile
    profile = config.profiles.get(selected)
    if profile is None:
        if selected == DEFAULT_PROFILE:
            profile = ClientProfile()
        else:
            msg = f"profile not found: {selected}"
            raise KeyError(msg)
    if not apply_env:
        return profile
    return _apply_environment_overrides(profile)


def set_profile(
    profile: ClientProfile,
    *,
    activate: bool = True,
    replace_legacy: bool = False,
) -> ClientProfile:
    config = _load_client_config(replace_legacy=replace_legacy)
    config.profiles[profile.name] = profile
    if activate:
        config.active_profile = profile.name
    save_config(config)
    return profile


def activate_profile(name: str) -> ClientProfile:
    profile = get_profile(name, apply_env=False)
    config = _load_client_config()
    config.active_profile = profile.name
    save_config(config)
    return profile


def delete_profile(name: str) -> None:
    if name == DEFAULT_PROFILE:
        msg = "the default profile cannot be deleted"
        raise ValueError(msg)
    config = _load_client_config()
    config.profiles.pop(name, None)
    if config.active_profile == name:
        config.active_profile = DEFAULT_PROFILE
    save_config(config)


def _load_client_config(*, replace_legacy: bool = False) -> ClientConfig:
    path = settings().config_path
    if not path.exists():
        return ClientConfig()
    with path.open("r", encoding="utf-8") as handle:
        raw_loaded = yaml.safe_load(handle)
    if raw_loaded is None:
        loaded: dict[str, JsonValue] = {}
    else:
        try:
            loaded = validate_json_object(raw_loaded)
        except ValueError as exc:
            msg = f"config file must contain an object: {path}"
            raise ConfigError(msg) from exc
    if not isinstance(loaded, dict):
        msg = f"config file must contain an object: {path}"
        raise ConfigError(msg)
    if _is_legacy_context_config(loaded):
        if replace_legacy:
            return ClientConfig()
        msg = "legacy context config found; run `lazycloud login` to write a profile config"
        raise ConfigError(msg)
    raw_profiles = loaded.get("profiles")
    if raw_profiles is None:
        profiles_value = empty_config()["profiles"]
        if not isinstance(profiles_value, dict):
            raise ConfigError("default profiles must be an object")
        raw_profiles = profiles_value
    elif not isinstance(raw_profiles, dict):
        msg = "config profiles must be an object"
        raise ConfigError(msg)
    normalized_profiles: dict[str, ClientProfile] = {}
    for profile_name, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, dict):
            msg = f"profile config must be an object: {profile_name}"
            raise ConfigError(msg)
        normalized_profiles[str(profile_name)] = ClientProfile.model_validate(
            {**raw_profile, "name": str(profile_name)}
        )
    normalized_profiles.setdefault(DEFAULT_PROFILE, ClientProfile())
    return ClientConfig.model_validate({**loaded, "profiles": normalized_profiles})


def _normalize_config(config: ClientConfig | Mapping[str, JsonValue]) -> ClientConfig:
    if isinstance(config, ClientConfig):
        normalized = config
    else:
        raw_profiles = config.get("profiles", {})
        if not isinstance(raw_profiles, dict):
            msg = "config profiles must be an object"
            raise ConfigError(msg)
        profiles: dict[str, ClientProfile] = {}
        for name, raw in raw_profiles.items():
            if not isinstance(raw, dict):
                msg = f"profile config must be an object: {name}"
                raise ConfigError(msg)
            profiles[str(name)] = ClientProfile.model_validate({**raw, "name": str(name)})
        profiles.setdefault(DEFAULT_PROFILE, ClientProfile())
        normalized = ClientConfig.model_validate({**config, "profiles": profiles})
    normalized.profiles.setdefault(DEFAULT_PROFILE, ClientProfile())
    return normalized


def _config_data(config: ClientConfig) -> dict[str, JsonValue]:
    return validate_json_object(
        {
            "active_profile": config.active_profile,
            "profiles": {
                name: profile.model_dump(mode="json", exclude={"name"})
                for name, profile in config.profiles.items()
            },
        }
    )


def _is_legacy_context_config(config: Mapping[str, JsonValue]) -> bool:
    return "contexts" in config or "current_context" in config


def _apply_environment_overrides(profile: ClientProfile) -> ClientProfile:
    current_settings = settings()
    updates: dict[str, object] = {}
    if current_settings.endpoint.strip():
        updates["endpoint"] = current_settings.endpoint.strip()
    if current_settings.workspace.strip():
        updates["workspace"] = current_settings.workspace.strip()
    if current_settings.token.strip():
        updates["token"] = current_settings.token.strip()
    if current_settings.tls is not None:
        updates["tls"] = current_settings.tls
    if not updates:
        return profile
    return profile.model_copy(update=updates)


__all__ = [
    "DEFAULT_PROFILE",
    "DEFAULT_WORKSPACE",
    "PACKAGED_DEFAULT_ENDPOINT",
    "ClientConfig",
    "ClientProfile",
    "ClientSettings",
    "ConfigError",
    "activate_profile",
    "active_profile_name",
    "delete_profile",
    "empty_config",
    "get_profile",
    "list_profiles",
    "load_config",
    "reset_settings_cache",
    "save_config",
    "set_profile",
    "settings",
]
