
from app.connectors.fabric_lakehouse import FabricLakehouseConnector


def main():
    connector = FabricLakehouseConnector()

    # Select the schema to discover
    schema_name = "saleslt"

    try:
        # Get all tables in the selected schema
        tables = connector.list_tables(schema_name)

        print(f"\n{'=' * 70}")
        print(f"SCHEMA: {schema_name}")
        print(f"TOTAL TABLES: {len(tables)}")
        print(f"{'=' * 70}")

        # Retrieve column metadata for every table
        for table in tables:
            table_name = table["name"]

            print(f"\nTable: {table_name}")
            print("-" * 70)

            metadata = connector.get_table_metadata(
                schema_name,
                table_name,
            )

            columns = metadata.get("columns", [])

            if not columns:
                print("No column metadata returned.")
                print("Available fields:", list(metadata.keys()))
                continue

            print(f"Total columns: {len(columns)}")
            print(f"{'Position':<10} {'Column Name':<30} {'Data Type':<20}")
            print("-" * 65)

            for column in sorted(
                columns,
                key=lambda item: item.get("position", 0),
            ):
                print(
                    f"{str(column.get('position', '-')):<10} "
                    f"{column.get('name', 'Unknown'):<30} "
                    f"{column.get('type_name', 'Unknown'):<20}"
                )

    except Exception as error:
        print(f"Fabric discovery failed: {error}")


if __name__ == "__main__":
    main()
