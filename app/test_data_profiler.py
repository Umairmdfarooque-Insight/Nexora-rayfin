
from pprint import pprint

from app.connectors.fabric_sql_connector import FabricSQLConnector
from app.discovery.data_profiler import profile_product_ids


def main():
    print("Connecting to Fabric SQL...")
    connector = FabricSQLConnector()

    print("Profiling product IDs...")
    result = profile_product_ids(connector)

    print("\n=== SALES TABLE: product_id samples ===")
    pprint(result["sales_product_ids"])

    print("\n=== PRODUCT TABLE: ProductID samples ===")
    pprint(result["dimension_product_ids"])

    sales_ids = {
        str(value).strip()
        for value in result["sales_product_ids"]
    }
    product_ids = {
        str(value).strip()
        for value in result["dimension_product_ids"]
    }

    print("\n=== SAMPLE COMPARISON ===")
    print("Matching sample values:", sales_ids.intersection(product_ids))
    print("Sales sample values:", sales_ids)
    print("Product sample values:", product_ids)


if __name__ == "__main__":
    main()
