
import sys

from app.connectors.fabric_lakehouse import FabricLakehouseConnector
from app.discovery.metadata_inventory import build_metadata_inventory


def main():
    connector = FabricLakehouseConnector()

    # Defaults to the schema we have already tested; pass another as the first argument
    schema_name = sys.argv[1] if len(sys.argv) > 1 else "saleslt"

    try:
        inventory = build_metadata_inventory(
            connector=connector,
            schema_name=schema_name,
        )

        print("\n--- METADATA INVENTORY ---")
        print(f"Schema: {inventory['schema_name']}")
        print(f"Total tables discovered: {len(inventory['tables'])}")

        for table in inventory["tables"]:
            print(f"\nTable: {table['table_name']}")
            print(f"Total columns: {len(table['columns'])}")

            for column in table["columns"]:
                print(
                    f"  {column['name']} "
                    f"| Type: {column['data_type']} "
                    f"| Nullable: {column['nullable']}"
                )

        if inventory["errors"]:
            print("\n--- DISCOVERY ERRORS ---")
            for error in inventory["errors"]:
                print(
                    f"Table: {error['table_name']} "
                    f"| Error: {error['error']}"
                )

    except Exception as error:
        print(f"Metadata inventory failed: {error}")


if __name__ == "__main__":
    main()
