from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(protected_namespaces=())


class ProviderName(StrEnum):
    OPENROUTER = "openrouter"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    OTHER = "other"


class ModelSelection(ApiModel):
    provider: ProviderName
    model: str

    @field_validator("model")
    @classmethod
    def _non_empty_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


class ModelPolicy(ApiModel):
    default: ModelSelection = Field(
        default_factory=lambda: ModelSelection(
            provider=ProviderName.OPENROUTER,
            model="gpt-4o-mini",
        )
    )
    roles: dict[str, ModelSelection] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        if "default" in value or "roles" in value:
            return value
        if not value:
            return {
                "default": {
                    "provider": ProviderName.OPENROUTER,
                    "model": "gpt-4o-mini",
                },
                "roles": {},
            }
        roles: dict[str, object] = {}
        for role, selection in value.items():
            if isinstance(selection, str):
                roles[str(role)] = {
                    "provider": ProviderName.OPENROUTER,
                    "model": selection,
                }
                continue
            if isinstance(selection, dict):
                roles[str(role)] = selection
                continue
            raise ValueError("model policy values must be strings or objects")
        return {
            "default": {
                "provider": ProviderName.OPENROUTER,
                "model": "gpt-4o-mini",
            },
            "roles": roles,
        }

    @field_validator("roles")
    @classmethod
    def _non_empty_roles(cls, value: dict[str, ModelSelection]) -> dict[str, ModelSelection]:
        for role in value:
            if not str(role).strip():
                raise ValueError("role names must not be empty")
        return value


def resolve(policy: ModelPolicy, role: str) -> ModelSelection:
    return policy.roles.get(role, policy.default)


class ModelCatalogProvider(ApiModel):
    provider: ProviderName
    models: list[str]


class ModelCatalog(ApiModel):
    providers: list[ModelCatalogProvider]


MODEL_CATALOG = ModelCatalog(
    providers=[
        ModelCatalogProvider(
            provider=ProviderName.OPENROUTER,
            models=["openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet"],
        ),
        ModelCatalogProvider(
            provider=ProviderName.ANTHROPIC,
            models=["claude-3-5-sonnet-latest", "claude-3-5-haiku-latest"],
        ),
        ModelCatalogProvider(
            provider=ProviderName.OPENAI,
            models=["gpt-4o-mini", "gpt-4.1-mini"],
        ),
        ModelCatalogProvider(provider=ProviderName.OTHER, models=[]),
    ]
)
