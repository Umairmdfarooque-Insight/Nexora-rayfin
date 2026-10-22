
import json
from typing import List

from pydantic import BaseModel, Field

from app.config import AZURE_OPENAI_DEPLOYMENT
from app.llm.client import client


class RequirementSpec(BaseModel):
    business_objective: str
    business_entities: List[str] = Field(default_factory=list)
    required_metrics: List[str] = Field(default_factory=list)
    dimensions: List[str] = Field(default_factory=list)
    filters: List[str] = Field(default_factory=list)
    security_requirements: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    clarification_questions: List[str] = Field(default_factory=list)


def analyze_requirement(business_requirement: str) -> RequirementSpec:
    """Convert a business requirement into a validated specification."""

    if not business_requirement.strip():
        raise ValueError("Business requirement cannot be empty.")


    instructions = """
    You are Nexora's Requirement Analyzer for Microsoft Fabric.

    Return a valid JSON object containing exactly these fields:
    - business_objective: string
    - business_entities: array of strings
    - required_metrics: array of strings
    - dimensions: array of strings
    - filters: array of strings
    - security_requirements: array of strings
    - assumptions: array of strings
    - clarification_questions: array of strings

    Rules:
    1. Extract what the user explicitly requested.
    2. Put inferred details in assumptions, not explicit requirements.
    3. Ask clarification questions when important information is missing.
    4. Do not invent actual database table or column names.
    5. Identify requested metrics, business entities, dimensions,
    filters, and security needs where applicable.
    6. Return JSON only, without Markdown or code fences.
    """


    
    response = client.responses.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        instructions=(
            "You are Nexora's Requirement Analyzer for Microsoft Fabric. "
            "Return your answer as a valid JSON object. "
            "The output must contain the business objective, business entities, "
            "required metrics, dimensions, filters, security requirements, "
            "assumptions, and clarification questions. "
            "Do not invent database table or column names. "
            "Return JSON only."
        ),
        input=(
            "Analyze the following business requirement and return the result "
            "in JSON format, with no Markdown or code fences:\n\n"
            f"{business_requirement}"
        ),
        text={"format": {"type": "json_object"}},
    )

    output = response.output_text

    if not output:
        raise RuntimeError("The model returned an empty response.")

    try:
        result = json.loads(output)
    except json.JSONDecodeError as error:
        raise ValueError(
            "The model did not return valid JSON."
        ) from error

        # Ensure security_requirements is a list of strings.
    security_requirements = result.get("security_requirements", [])

    if isinstance(security_requirements, dict):
        result["security_requirements"] = [
            f"{key}: {value}"
            for key, value in security_requirements.items()
        ]
    elif isinstance(security_requirements, str):
        result["security_requirements"] = [security_requirements]

    return RequirementSpec.model_validate(result)
