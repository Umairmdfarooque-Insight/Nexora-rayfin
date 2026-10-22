
import json

from app.llm.requirement_analyzer import analyze_requirement


def main() -> None:
    requirement = """
    Create a Power BI sales dashboard showing:
    - Total sales by month
    - Sales by product category
    - Top 10 customers by revenue
    - Year-over-year sales growth
    """

    try:
        specification = analyze_requirement(requirement)

        print("\n--- NEXORA'S REQUIREMENT SPECIFICATION ---\n")
        print(
            json.dumps(
                specification.model_dump(),
                indent=2,
                ensure_ascii=False,
            )
        )

    except Exception as error:
        print(f"Requirement analysis failed: {error}")


if __name__ == "__main__":
    main()
