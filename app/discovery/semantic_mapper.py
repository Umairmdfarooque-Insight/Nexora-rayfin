
import json
from typing import Any

from app.config import AZURE_OPENAI_DEPLOYMENT
from app.llm.client import client
from app.llm.requirement_analyzer import RequirementSpec


def suggest_semantic_mappings(
    requirement: RequirementSpec,
    metadata_inventory: dict[str, Any],
) -> dict[str, Any]:
    """
    Suggest semantic mappings using Azure AI Foundry.

    Metadata is the source of truth for table and column existence.
    AI suggestions remain candidates until business rules are validated.
    """

    # 1. Prepare metadata from the actual Fabric inventory.
    available_metadata = []

    for table in metadata_inventory.get("tables", []):
        available_metadata.append({
            "table_name": table.get("table_name"),
            "columns": [
                {
                    "name": column.get("name"),
                    "data_type": column.get("data_type"),
                }
                for column in table.get("columns", [])
            ],
        })

    requirement_data = requirement.model_dump()

    # 2. Ask the model for semantic candidates, not confirmed relationships.
    instructions = """
    You are Nexora's Semantic Data Mapper, the data discovery component
    behind Rayfin.

    Map the supplied business requirement to the supplied Fabric metadata.

    Return one valid JSON object with this structure:
    {
      "mappings": [
        {
          "business_concept": "string",
          "category": "metric or dimension",
          "status": "candidate_match or unmatched",
          "confidence": "high, medium, or low",
          "candidates": [
            {
              "table_name": "string",
              "column_name": "string",
              "reason": "string",
              "aggregation": "SUM, COUNT, NONE, or another justified proposal",
              "validation_status": "metadata_verified or business_confirmation_required"
            }
          ],
          "relationships": [
            {
              "from_table": "string",
              "from_column": "string",
              "to_table": "string",
              "to_column": "string",
              "reason": "string",
              "validation_status": "candidate_relationship"
            }
          ],
          "clarification_needed": "string"
        }
      ]
    }

    Rules:
    1. Use only the supplied tables and columns.
    2. Never invent table names, column names, or relationships.
    3. Consider business meaning, data types, and table context.
    4. Propose aggregation rules for metrics, but do not assume that
       a numeric column is automatically additive.
    5. Metadata verification proves existence only, not business meaning.
    6. Confidence must reflect the evidence available.
    7. Relationships are candidates, never confirmed foreign keys.
    8. Matching data types alone do not prove a valid relationship.
    9. Prefer descriptive attributes for product and customer labels
       when the supplied metadata supports them.
    10. Return multiple candidates when appropriate.
    11. If no suitable candidate exists, return unmatched with no candidates.
    12. State clarification needs where definitions or reporting periods
        are unclear.
    13. Do not generate SQL.
    14. Return JSON only, without Markdown or code fences.
    """

    response = client.responses.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        instructions=instructions,
        input=(
            "Analyze the requirement and metadata below. "
            "Return a JSON object matching the required structure.\n\n"
            + json.dumps({
                "business_requirement": requirement_data,
                "metadata_inventory": available_metadata,
            })
        ),
        text={"format": {"type": "json_object"}},
    )

    output = response.output_text

    if not output:
        raise RuntimeError(
            "Azure AI Foundry returned an empty semantic mapping response."
        )

    try:
        result = json.loads(output)
    except json.JSONDecodeError as error:
        raise ValueError(
            "The semantic mapper returned invalid JSON."
        ) from error

    mappings = result.get("mappings")

    if not isinstance(mappings, list):
        raise ValueError(
            "The semantic mapper response must contain a mappings list."
        )

    # 3. Build authoritative lookup structures from the inventory.
    valid_columns = {}
    for table in available_metadata:
        table_name = table.get("table_name")
        for column in table.get("columns", []):
            valid_columns[(table_name, column.get("name"))] = column

    valid_tables = {
        table.get("table_name")
        for table in available_metadata
    }

    # 4. Validate candidates and relationships independently.
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise ValueError("Each mapping must be a JSON object.")

        candidates = mapping.get("candidates", [])
        relationships = mapping.get("relationships", [])

        if not isinstance(candidates, list):
            raise ValueError("Mapping candidates must be a list.")

        if not isinstance(relationships, list):
            raise ValueError("Mapping relationships must be a list.")

        verified_candidates = []

        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError("Each candidate must be a JSON object.")

            identity = (
                candidate.get("table_name"),
                candidate.get("column_name"),
            )

            if identity not in valid_columns:
                raise ValueError(
                    "The AI suggested a table or column that does not "
                    f"exist in the inventory: {identity}"
                )

            # Existence is verified; business meaning is not.
            candidate["validation_status"] = (
                "metadata_verified_business_review_required"
            )
            verified_candidates.append(candidate)

        mapping["candidates"] = verified_candidates

        verified_relationships = []

        for relationship in relationships:
            if not isinstance(relationship, dict):
                raise ValueError(
                    "Each relationship must be a JSON object."
                )

            source = (
                relationship.get("from_table"),
                relationship.get("from_column"),
            )
            target = (
                relationship.get("to_table"),
                relationship.get("to_column"),
            )

            source_exists = source in valid_columns
            target_exists = target in valid_columns

            if not source_exists or not target_exists:
                raise ValueError(
                    "The AI suggested a relationship with a missing "
                    f"table or column: {source} -> {target}"
                )

            source_type = str(
                valid_columns[source].get("data_type", "")
            ).lower()
            target_type = str(
                valid_columns[target].get("data_type", "")
            ).lower()

            relationship["validation_status"] = (
                "metadata_verified_business_review_required"
                if source_type == target_type
                else "type_mismatch_review_required"
            )

            verified_relationships.append(relationship)

        mapping["relationships"] = verified_relationships

    # 5. Return mappings alongside the inventory's existing error list.
    return {
        "schema_name": metadata_inventory.get("schema_name"),
        "business_objective": requirement.business_objective,
        "mappings": mappings,
        "metadata_errors": metadata_inventory.get("errors", []),
    }