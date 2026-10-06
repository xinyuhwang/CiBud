"""Provider-agnostic interface for schema-constrained LLM steps (design doc §3, §14).

Every LLM step returns a validated Pydantic object plus the provenance needed to reproduce it.
Concrete providers implement ``LLMProvider``; the model is chosen per step.
"""

from dataclasses import dataclass, field
from string import Template
from typing import Any, Protocol

from pydantic import BaseModel

from cibud.models.common import LLMProvenance


@dataclass(frozen=True)
class Prompt:
    """A versioned prompt. Bump ``version`` whenever the wording changes."""

    name: str
    version: str
    system: str
    template: str  # ``string.Template`` syntax: $variable

    def render(self, **variables: Any) -> str:
        return Template(self.template).substitute(**variables)


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    output: T
    provenance: LLMProvenance
    usage: dict[str, int] = field(default_factory=dict)


class LLMError(Exception):
    """The provider failed or returned output that does not match the schema."""


class LLMProvider(Protocol):
    async def generate[T: BaseModel](
        self,
        *,
        prompt: Prompt,
        variables: dict[str, Any],
        schema: type[T],
        model: str,
        max_tokens: int = 4096,
    ) -> StructuredResult[T]: ...
