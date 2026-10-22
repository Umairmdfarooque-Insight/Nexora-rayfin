"""T-103 live check: connectors built from a run's scope reach Fabric.

Run: python -m app.manual_tests.t103_connectors_check <run_id>
Expected: the Lakehouse's catalog name and schema list (including saleslt),
then "Connected successfully." for the scope's SQL endpoint. Needs a run whose
scope was set with PUT /runs/{id}/scope, and a browser sign-in the first time.
"""

import sys

from app.connectors.fabric_lakehouse import FabricLakehouseConnector
from app.connectors.fabric_sql_connector import FabricSQLConnector
from app.store.base import RunNotFound
from app.store.factory import get_run_store


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python -m app.manual_tests.t103_connectors_check <run_id>")
        return 2
    try:
        run = get_run_store().get(sys.argv[1])
    except RunNotFound as error:
        print(error)
        return 1
    if run.scope is None:
        print("This run has no scope yet. Call PUT /runs/<run_id>/scope first.")
        return 1

    lakehouse = FabricLakehouseConnector.for_scope(run.scope)
    print(lakehouse.catalog_name, [s["name"] for s in lakehouse.list_schemas()])
    FabricSQLConnector.for_scope(run.scope).test_connection()
    return 0


if __name__ == "__main__":
    sys.exit(main())
