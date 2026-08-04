from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar, get_args

from pydantic import SecretBytes, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    YamlConfigSettingsSource,
)

from shared.app_identity import ENV_PREFIX

CONFIG_DIR_ENV = f"{ENV_PREFIX}_CONFIG_DIR"


class YamlLayeredSettings(BaseSettings):
    """Settings that read an owner-scoped YAML layer beneath the environment.

    Precedence is ``init > env > yaml > secrets``. The environment carries
    secrets and everything that legitimately differs per environment or changes
    while a deployment runs, so it outranks a file whose job is to state what is
    normally identical everywhere.

    ``LAZYCLOUD_CONFIG_DIR`` addresses a directory holding one file per owner.
    Unset reads no YAML at all: with no default path, no file a machine happens
    to carry can change what a process—or the suite—observes.
    """

    yaml_file_name: ClassVar[str] = ""
    yaml_section: ClassVar[str] = ""
    env_only_fields: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: object) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        if not cls.yaml_file_name or not cls.yaml_section:
            raise TypeError(f"{cls.__name__} must declare yaml_file_name and yaml_section")
        # A misspelling here silently opens a credential to the config file, and
        # nothing downstream would notice, so it is rejected at import.
        unknown = cls.env_only_fields - set(cls.model_fields)
        if unknown:
            raise TypeError(
                f"{cls.__name__}.env_only_fields names settings it does not declare: "
                + ", ".join(sorted(unknown))
            )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del dotenv_settings
        return (
            init_settings,
            env_settings,
            cls._yaml_layer(settings_cls),
            file_secret_settings,
        )

    @classmethod
    def _yaml_layer(cls, settings_cls: type[BaseSettings]) -> PydanticBaseSettingsSource:
        directory = _configuration_directory()
        if directory is None:
            return YamlConfigSettingsSource(settings_cls, yaml_file=None)
        path = directory / cls.yaml_file_name
        if not path.is_file():
            # One directory serves every owner. An owner that ships no file has
            # nothing to override, not an unsatisfied dependency.
            return YamlConfigSettingsSource(settings_cls, yaml_file=None)
        try:
            layer = YamlConfigSettingsSource(
                settings_cls,
                yaml_file=path,
                yaml_config_section=cls.yaml_section,
            )
        except KeyError:
            # A file states only the sections it overrides; the library reports
            # an absent one as a failed lookup.
            return YamlConfigSettingsSource(settings_cls, yaml_file=None)
        except AttributeError as exc:
            raise ValueError(
                f"{path}: '{cls.yaml_section}' must hold a mapping of settings"
            ) from exc
        env_only = frozenset(layer.yaml_data) & cls._env_only_names()
        if env_only:
            raise ValueError(
                f"{path}: '{cls.yaml_section}' must not carry "
                f"{', '.join(sorted(env_only))}; the environment is their only source"
            )
        return layer

    @classmethod
    def _env_only_names(cls) -> frozenset[str]:
        declared_secrets = {
            name for name, field in cls.model_fields.items() if _is_secret(field.annotation)
        }
        return cls.env_only_fields | declared_secrets


def _is_secret(annotation: object) -> bool:
    """Whether a field holds a secret, including one that may be absent.

    A credential is as often declared ``SecretStr | None`` as ``SecretStr``, and
    reading only the bare type would leave the optional spelling out of the
    automatic protection—silently, and for exactly the fields most likely to be
    optional because they are not always configured.
    """
    if isinstance(annotation, type):
        return issubclass(annotation, SecretStr | SecretBytes)
    return any(_is_secret(argument) for argument in get_args(annotation))


def _configuration_directory() -> Path | None:
    configured = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if not configured:
        return None
    directory = Path(configured).expanduser()
    if not directory.is_dir():
        # An unmounted volume or a mistyped path would otherwise leave every
        # owner on its defaults while the deployment looks configured.
        raise ValueError(f"{CONFIG_DIR_ENV} names {directory}, which is not a directory")
    return directory


__all__ = [
    "CONFIG_DIR_ENV",
    "YamlLayeredSettings",
]
