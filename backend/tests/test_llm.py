import pytest
from pydantic import BaseModel

from cibud.llm import FakeProvider, LLMError, Prompt

PROMPT = Prompt(
    name="summarize", version="v1", system="You summarize.", template="Summarize: $text"
)


class Summary(BaseModel):
    summary: str


async def test_returns_validated_output_with_provenance() -> None:
    llm = FakeProvider()
    llm.push({"summary": "short"})
    result = await llm.generate(
        prompt=PROMPT, variables={"text": "long"}, schema=Summary, model="test-model"
    )
    assert result.output == Summary(summary="short")
    assert result.provenance.model == "test-model"
    assert result.provenance.prompt_version == "v1"
    assert llm.calls[0].rendered == "Summarize: long"


async def test_schema_mismatch_raises() -> None:
    llm = FakeProvider()
    llm.push({"wrong_field": 1})
    with pytest.raises(LLMError):
        await llm.generate(prompt=PROMPT, variables={"text": "x"}, schema=Summary, model="m")


def test_missing_template_variable_raises() -> None:
    with pytest.raises(KeyError):
        PROMPT.render()
