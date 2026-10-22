
from typing import Any

from app.discovery.glossary import load_glossary, normalize  # noqa: F401  (normalize is re-exported)
from app.llm.requirement_analyzer import RequirementSpec


def match_requirements_to_data(
    requirement: RequirementSpec,
    metadata_inventory: dict[str, Any],
) -> dict[str, Any]:
    """Match business concepts to real Fabric metadata."""

    glossary = load_glossary()
    available = []

    for table in metadata_inventory.get("tables", []):
        table_name = table.get("table_name", "")

        for column in table.get("columns", []):
            column_name = column.get("name", "")

            available.append({
                "table_name": table_name,
                "column_name": column_name,
                "data_type": column.get("data_type"),
            })

    requested_items = []

    for category, values in [
        ("business_entity", requirement.business_entities),
        ("metric", requirement.required_metrics),
        ("dimension", requirement.dimensions),
        ("filter", requirement.filters),
    ]:
        for value in values:
            requested_items.append({
                "category": category,
                "name": value,
            })

    matches = []
    unmatched = []

    for item in requested_items:
        requested = item["name"]
        aliases = glossary.aliases_for(requested)

        candidates = []

        for source in available:
            table = normalize(source["table_name"])
            column = normalize(source["column_name"])

            # Use targeted alias matching rather than matching
            # every column containing a broad word such as "sales".
            if any(
                alias
                and (
                    alias == column
                    or alias == table
                    or alias in column
                    or alias in table
                )
                for alias in aliases
            ):
                candidates.append(source)

        # Remove duplicate candidates while preserving order.
        unique_candidates = []
        seen = set()

        for candidate in candidates:
            identity = (
                candidate["table_name"],
                candidate["column_name"],
            )
            if identity not in seen:
                seen.add(identity)
                unique_candidates.append(candidate)

        if unique_candidates:
            matches.append({
                "category": item["category"],
                "requested_item": requested,
                "status": "candidate_match",
                "candidates": unique_candidates,
            })
        else:
            unmatched.append({
                "category": item["category"],
                "requested_item": requested,
                "status": "unmatched",
            })

    return {
        "schema_name": metadata_inventory.get("schema_name"),
        "business_objective": requirement.business_objective,
        "matches": matches,
        "unmatched": unmatched,
        "metadata_errors": metadata_inventory.get("errors", []),
        "summary": {
            "requested_items": len(requested_items),
            "candidate_matches": len(matches),
            "unmatched_items": len(unmatched),
        },
    }
