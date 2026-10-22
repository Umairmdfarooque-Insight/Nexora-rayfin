
import json

from app.connectors.fabric_lakehouse import FabricLakehouseConnector
from app.discovery.metadata_inventory import build_metadata_inventory
from app.discovery.requirement_data_matcher import (
    match_requirements_to_data,
)
from app.llm.requirement_analyzer import analyze_requirement


def main():
    business_requirement = (
        "Create a sales report showing total sales by product "
        "and customer."
    )

    # Step 1: Analyze the business requirement.
    requirement = analyze_requirement(business_requirement)

    print("\n=== ANALYZED REQUIREMENT ===")
    print(json.dumps(requirement.model_dump(), indent=2))

    # Step 2: Discover the real Fabric metadata.
    connector = FabricLakehouseConnector()
    schema_name = "saleslt"

    inventory = build_metadata_inventory(
        connector,
        schema_name,
    )

    print("\n=== METADATA INVENTORY SUMMARY ===")
    print("Schema:", inventory["schema_name"])
    print("Tables discovered:", len(inventory["tables"]))

    # Step 3: Match requirements to discovered metadata.
    result = match_requirements_to_data(
        requirement,
        inventory,
    )

    print("\n=== MATCH SUMMARY ===")
    print(result["summary"])

    print("\n=== CANDIDATE MATCHES ===")
    for match in result["matches"]:
        print(
            f'{match["category"]}: {match["requested_item"]}'
        )
        for candidate in match["candidates"]:
            print(
                f'  -> {candidate["table_name"]}.'
                f'{candidate["column_name"]}'
            )

    print("\n=== UNMATCHED ITEMS ===")
    for item in result["unmatched"]:
        print(f'- {item["category"]}: {item["requested_item"]}')


if __name__ == "__main__":
    main()
