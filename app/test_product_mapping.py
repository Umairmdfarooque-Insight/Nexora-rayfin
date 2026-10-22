
from app.connectors.fabric_sql_connector import FabricSQLConnector


def main():
    connector = FabricSQLConnector()

    query = """
        SELECT
            COUNT(*) AS detail_rows,
            COUNT(DISTINCT d.ProductID) AS distinct_detail_products,
            COUNT(p.ProductID) AS matched_rows,
            COUNT(DISTINCT p.ProductID) AS matched_products
        FROM saleslt.SalesLT_SalesOrderDetail AS d
        LEFT JOIN saleslt.SalesLT_Product AS p
            ON d.ProductID = p.ProductID;
    """

    with connector.connect() as connection:
        cursor = connection.cursor()
        cursor.execute(query)
        row = cursor.fetchone()

    print("\\n=== PRODUCT RELATIONSHIP VALIDATION ===")
    print("Detail rows:", row[0])
    print("Distinct detail products:", row[1])
    print("Matched detail rows:", row[2])
    print("Matched distinct products:", row[3])


if __name__ == "__main__":
    main()