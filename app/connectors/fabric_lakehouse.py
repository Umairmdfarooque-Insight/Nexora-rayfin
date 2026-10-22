
from urllib.parse import quote

import requests

from app.auth import STORAGE_SCOPE, get_token
from app.config import (
    FABRIC_WORKSPACE_ID,
    FABRIC_LAKEHOUSE_ID,
    FABRIC_LAKEHOUSE_NAME,
)

ONELAKE_TABLE_API = "https://onelake.table.fabric.microsoft.com/delta"


class FabricLakehouseConnector:
    """Reads Lakehouse metadata through the OneLake table API.

    With no arguments it uses the workspace and Lakehouse from `.env`, for
    scripts that don't have a run. Runs use `for_scope`.
    """

    def __init__(
        self,
        workspace_id: str | None = None,
        lakehouse_id: str | None = None,
        lakehouse_name: str | None = None,
        token_provider=None,
    ):
        # With a token_provider (the API's on-behalf-of token for the signed-in
        # user) it is used instead of the local interactive credential.
        self._token_provider = token_provider
        self.workspace_id = workspace_id or FABRIC_WORKSPACE_ID
        self.lakehouse_id = lakehouse_id or FABRIC_LAKEHOUSE_ID
        self.lakehouse_name = lakehouse_name or FABRIC_LAKEHOUSE_NAME

    @classmethod
    def for_scope(cls, scope, token_provider=None) -> "FabricLakehouseConnector":
        """Connector for a run's scope (a RunScope or an equivalent dict)."""
        get = scope.get if isinstance(scope, dict) else lambda k: getattr(scope, k)
        return cls(
            get("workspace_id"),
            get("lakehouse_id"),
            get("lakehouse_name"),
            token_provider,
        )

    def _get_headers(self) -> dict:
        token = (
            self._token_provider() if self._token_provider else get_token(STORAGE_SCOPE)
        )

        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    @property
    def lakehouse_path(self) -> str:
        return (
            f"{quote(self.workspace_id, safe='')}/"
            f"{quote(self.lakehouse_id, safe='')}"
        )

    @property
    def catalog_name(self) -> str:
        return f"{self.lakehouse_name}.Lakehouse"

    def list_schemas(self) -> list[dict]:
        url = (
            f"{ONELAKE_TABLE_API}/{self.lakehouse_path}/"
            "api/2.1/unity-catalog/schemas"
        )

        response = requests.get(
            url,
            headers=self._get_headers(),
            params={"catalog_name": self.catalog_name},
            timeout=30,
        )

        # Diagnostic output to identify the API error
        print("\n--- SCHEMA DISCOVERY DEBUG ---")
        print("Request URL:", response.url)
        print("Status:", response.status_code)
        print("Response:", response.text)
        print("--- END DEBUG ---\n")

        response.raise_for_status()

        return response.json().get("schemas", [])


    def list_tables(self, schema_name: str) -> list[dict]:
        url = (
            f"{ONELAKE_TABLE_API}/{self.lakehouse_path}/"
            "api/2.1/unity-catalog/tables"
        )

        tables: list[dict] = []
        seen_tokens: set[str] = set()
        params = {
            "catalog_name": self.catalog_name,
            "schema_name": schema_name,
        }

        # Follow next_page_token until the listing ends. A repeated token would
        # loop forever, so it stops the listing instead.
        while True:
            response = requests.get(
                url,
                headers=self._get_headers(),
                params=params,
                timeout=30,
            )
            response.raise_for_status()

            body = response.json()
            tables.extend(body.get("tables", []))

            token = body.get("next_page_token")
            if not token or token in seen_tokens:
                return tables
            seen_tokens.add(token)
            params = {**params, "page_token": token}

    def get_table_metadata(
        self, schema_name: str, table_name: str
    ) -> dict:
        full_table_name = (
            f"{self.catalog_name}.{schema_name}.{table_name}"
        )

        url = (
            f"{ONELAKE_TABLE_API}/{self.lakehouse_path}/"
            "api/2.1/unity-catalog/tables/"
            f"{quote(full_table_name, safe='')}"
        )

        response = requests.get(
            url,
            headers=self._get_headers(),
            timeout=30,
        )

        response.raise_for_status()

        return response.json()
    
    def discover_schema_metadata(self) -> list[dict]:
        """Discover tables and columns from the saleslt schema."""
        schema_name = "saleslt"
        tables = self.list_tables(schema_name)
        discovered_tables = []

        for table in tables:
            table_name = table["name"]

            metadata = self.get_table_metadata(
                schema_name,
                table_name,
            )

            columns = [
                {
                    "name": column["name"],
                    "data_type": column.get("type_name"),
                    "nullable": column.get("nullable"),
                }
                for column in metadata.get("columns", [])
            ]

            discovered_tables.append(
                {
                    "schema": schema_name,
                    "table_name": table_name,
                    "column_count": len(columns),
                    "columns": columns,
                }
            )

        return discovered_tables
