"""Deterministic provider for tests: returns queued responses and records every call."""

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from cibud.llm.base import LLMError, Prompt, StructuredResult
from cibud.models.common import LLMProvenance


@dataclass
class FakeCall:
    prompt: Prompt
    rendered: str
    schema: type[BaseModel]
    model: str


@dataclass
class FakeProvider:
    """Queue raw dict responses with ``push``; each ``generate`` call pops one."""

    responses: list[dict[str, Any]] = field(default_factory=list)
    calls: list[FakeCall] = field(default_factory=list)

    def push(self, *responses: dict[str, Any]) -> None:
        self.responses.extend(responses)

    async def generate[T: BaseModel](
        self,
        *,
        prompt: Prompt,
        variables: dict[str, Any],
        schema: type[T],
        model: str,
        max_tokens: int = 4096,
    ) -> StructuredResult[T]:
        rendered = prompt.render(**variables)
        self.calls.append(FakeCall(prompt, rendered, schema, model))
        if not self.responses:
            raise LLMError("FakeProvider has no queued responses")
        try:
            output = schema.model_validate(self.responses.pop(0))
        except ValidationError as exc:
            raise LLMError(f"response does not match {schema.__name__}") from exc
        provenance = LLMProvenance(
            model=model, prompt_name=prompt.name, prompt_version=prompt.version
        )
        return StructuredResult(output=output, provenance=provenance)
