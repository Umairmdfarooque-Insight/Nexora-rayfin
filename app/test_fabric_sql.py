from app.connectors.fabric_sql_connector import FabricSQLConnector


def main():
    print("Testing Fabric SQL connection...")

    connector = FabricSQLConnector()
    connector.test_connection()


if __name__ == "__main__":
    main()