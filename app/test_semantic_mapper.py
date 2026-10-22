
from app.discovery.relationship_validator import validate_relationships

from app.connectors.fabric_lakehouse import FabricLakehouseConnector
from app.discovery.metadata_inventory import build_metadata_inventory
from app.discovery.semantic_mapper import suggest_semantic_mappings
from app.llm.requirement_analyzer import analyze_requirement


def main():
    business_requirement = (
        "Create a sales report showing total sales "
        "by product and customer."
    )

    # Step 1: Analyze the business requirement
    print("Analyzing the business requirement...")
    requirement = analyze_requirement(business_requirement)

    # Step 2: Discover Fabric metadata
    print("Discovering Fabric metadata...")
    connector = FabricLakehouseConnector()

    inventory = build_metadata_inventory(
        connector,
        "saleslt",
    )

    # Step 3: Inspect relevant column data types
    print("\n=== COLUMN DATA TYPES ===")

    for table in inventory["tables"]:
        for column in table["columns"]:
            if (
                table["table_name"] == "synthetic_sales_daily"
                and column["name"] in [
                    "product_id",
                    "customer_email",
                    "total_sales",
                ]
            ) or (
                table["table_name"] == "SalesLT_Product"
                and column["name"] == "ProductID"
            ):
                print(
                    f'{table["table_name"]}.{column["name"]}'
                    f' -> {column["data_type"]}'
                )

    # Step 4: Generate AI-assisted semantic mappings
    print("\nRequesting AI-assisted semantic mappings...")

    result = suggest_semantic_mappings(
        requirement,
        inventory,
    )

    # Step 5: Validate suggested relationships
    result = validate_relationships(
        result,
        inventory,
    )

    # Step 6: Display mapping results
    print("\n=== SEMANTIC MAPPING RESULTS ===")

    for mapping in result["mappings"]:
        print(
            f'\n{mapping.get("category")}: '
            f'{mapping.get("business_concept")}'
        )

        print("Status:", mapping.get("status"))
        print("Confidence:", mapping.get("confidence"))

        for candidate in mapping.get("candidates", []):
            print(
                f'  -> {candidate["table_name"]}.'
                f'{candidate["column_name"]}'
            )
            print("     Reason:", candidate.get("reason"))
            print(
                "     Aggregation:",
                candidate.get("aggregation"),
            )
            print(
                "     Validation:",
                candidate.get("validation_status"),
            )

        for relationship in mapping.get("relationships", []):
            print(
                f'  Relationship: '
                f'{relationship.get("from_table")}.'
                f'{relationship.get("from_column")} -> '
                f'{relationship.get("to_table")}.'
                f'{relationship.get("to_column")}'
            )

            print(
                "     Status:",
                relationship.get("validation_status"),
            )

            if relationship.get("source_data_type") is not None:
                print(
                    "     Source type:",
                    relationship.get("source_data_type"),
                )

            if relationship.get("target_data_type") is not None:
                print(
                    "     Target type:",
                    relationship.get("target_data_type"),
                )

            if relationship.get("validation_reason"):
                print(
                    "     Reason:",
                    relationship.get("validation_reason"),
                )

        clarification = mapping.get("clarification_needed")

        if clarification:
            print("Clarification:", clarification)

    print("\nTotal mappings:", len(result["mappings"]))


if __name__ == "__main__":
    main()
