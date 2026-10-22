
from typing import Any

from app.connectors.fabric_lakehouse import FabricLakehouseConnector


def build_metadata_inventory(
    connector: FabricLakehouseConnector,
    schema_name: str,
) -> dict[str, Any]:
    """
    Discover every table and its column metadata
    within the specified Fabric Lakehouse schema.
    """

    inventory = {
        "schema_name": schema_name,
        "tables": [],
        "errors": [],
    }

    # Discover tables in the selected schema
    tables = connector.list_tables(schema_name)

    for table in tables:
        table_name = table["name"]

        try:
            # Retrieve metadata for each table
            metadata = connector.get_table_metadata(
                schema_name,
                table_name,
            )

            columns = []

            for column in metadata.get("columns", []):
                columns.append({
                    "name": column.get("name"),
                    "data_type": column.get("type_name"),
                    "nullable": column.get("nullable"),
                    "position": column.get("position"),
                })

            # Add the table and its columns to the inventory
            inventory["tables"].append({
                "table_name": table_name,
                "columns": columns,
            })

        except Exception as error:
            # Continue discovering other tables if one fails
            inventory["errors"].append({
                "table_name": table_name,
                "error": str(error),
            })

    return inventory
