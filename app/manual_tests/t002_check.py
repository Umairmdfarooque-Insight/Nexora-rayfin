"""T-002 manual test: one browser sign-in for both connectors.

Run from the project root: python -m app.manual_tests.t002_check
Expected: a single sign-in, then the table count and "Connected successfully."
Run it a second time within an hour: no sign-in prompt (an account picker is fine).
"""

from app.connectors.fabric_lakehouse import FabricLakehouseConnector
from app.connectors.fabric_sql_connector import FabricSQLConnector
from app.discovery.metadata_inventory import build_metadata_inventory


def main() -> None:
    inventory = build_metadata_inventory(FabricLakehouseConnector(), "saleslt")
    print("tables:", len(inventory["tables"]))
    FabricSQLConnector().test_connection()


if __name__ == "__main__":
    main()
