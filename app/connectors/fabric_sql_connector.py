import importlib.util

from app.auth import SQL_SCOPE, get_token
from app.config import (
    FABRIC_SQL_SERVER,
    FABRIC_SQL_DATABASE,
)

ODBC_DRIVER_NAME = "ODBC Driver 18 for SQL Server"


def _select_driver() -> str:
    """Returns "pyodbc" when the ODBC Driver 18 is installed, else "mssql_python"
    (Microsoft's pure-Python driver, for notebook runtimes that don't have the native
    ODBC driver installed). See fabric-hosting.md's platform-limits table: this hasn't
    yet been confirmed against a live Fabric notebook runtime."""
    try:
        import pyodbc

        if ODBC_DRIVER_NAME in pyodbc.drivers():
            return "pyodbc"
    except ImportError:
        pass

    if importlib.util.find_spec("mssql_python") is not None:
        return "mssql_python"

    raise RuntimeError(
        f"Neither {ODBC_DRIVER_NAME} (pyodbc) nor the mssql-python package is "
        "available in this runtime; install one of them to use FabricSQLConnector."
    )


class FabricSQLConnector:
    """Connects to a Fabric SQL endpoint.

    Defaults to the Lakehouse endpoint from FABRIC_SQL_*. Pass `server` and
    `database` to reach another one, such as the control database.
    """

    def __init__(
        self,
        server: str | None = None,
        database: str | None = None,
        token_provider=None,
    ):
        # With a token_provider (the API's on-behalf-of token for the signed-in
        # user) it is used instead of the local interactive credential.
        self._token_provider = token_provider
        self.server = server or FABRIC_SQL_SERVER
        self.database = database or FABRIC_SQL_DATABASE

    @classmethod
    def for_scope(cls, scope, token_provider=None) -> "FabricSQLConnector":
        """Connector for the SQL endpoint of a run's scope."""
        endpoint = (
            scope.get("sql_endpoint")
            if isinstance(scope, dict)
            else scope.sql_endpoint
        )
        if not endpoint:
            raise ValueError("The run's Lakehouse has no SQL endpoint yet")
        if not isinstance(endpoint, dict):
            endpoint = {"server": endpoint.server, "database": endpoint.database}
        return cls(endpoint["server"], endpoint["database"], token_provider)

    def connect(self):
        token = self._token_provider() if self._token_provider else get_token(SQL_SCOPE)

        # SQL Server ODBC expects the access token as UTF-16-LE bytes,
        # prefixed with its 4-byte length.
        token_bytes = token.encode("utf-16-le")
        token_struct = (
            len(token_bytes).to_bytes(4, byteorder="little")
            + token_bytes
        )

        driver = _select_driver()

        if driver == "pyodbc":
            import pyodbc

            connection_string = (
                f"Driver={{{ODBC_DRIVER_NAME}}};"
                f"Server=tcp:{self.server},1433;"
                f"Database={self.database};"
                "Encrypt=yes;"
                "TrustServerCertificate=no;"
                "Connection Timeout=30;"
            )
            return pyodbc.connect(
                connection_string,
                attrs_before={1256: token_struct},
                timeout=30,
            )

        import mssql_python

        connection_string = (
            f"Server=tcp:{self.server},1433;"
            f"Database={self.database};"
            "Encrypt=yes;"
            "TrustServerCertificate=no;"
        )
        return mssql_python.connect(
            connection_string,
            attrs_before={1256: token_struct},
            timeout=30,
        )

    def test_connection(self) -> None:
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT DB_NAME()")
            row = cursor.fetchone()
            print("Connected successfully.")
            print("Database:", row[0])
