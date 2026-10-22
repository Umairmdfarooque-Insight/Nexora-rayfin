
from app.llm.client import ask_rayfin


def main() -> None:
    business_requirement = """
    Create a Power BI sales dashboard that shows:
    - Total sales by month
    - Sales by product category
    - Top 10 customers by revenue
    - Year-over-year sales growth

    Identify the business entities, metrics, and likely data
    tables required. If information is missing, state your
    assumptions instead of inventing table names.
    """

    try:
        result = ask_rayfin(business_requirement)
        print("\n--- RAYFIN AI RESPONSE ---\n")
        print(result)

    except Exception as error:
        print(f"Rayfin AI test failed: {error}")


if __name__ == "__main__":
    main()
