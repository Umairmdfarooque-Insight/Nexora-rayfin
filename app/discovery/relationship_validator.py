
from typing import Any


def validate_relationships(
    semantic_mapping: dict[str, Any],
    metadata_inventory: dict[str, Any],
    connector: Any = None,
    run_data_checks: bool = False,
) -> dict[str, Any]:
    """
    Validate AI-suggested relationships against metadata and, optionally,
    actual data in Fabric.

    Metadata checks are always performed.
    Data checks run only when a SQL connector is supplied and enabled.
    """

    table_columns = {}

    for table in metadata_inventory.get("tables", []):
        table_name = table.get("table_name")
        table_columns[table_name] = {
            column.get("name"): column
            for column in table.get("columns", [])
        }

    validated_mappings = []

    for mapping in semantic_mapping.get("mappings", []):
        updated_mapping = dict(mapping)
        validated_relationships = []

        for original in mapping.get("relationships", []):
            relationship = dict(original)

            from_table = relationship.get("from_table")
            from_column = relationship.get("from_column")
            to_table = relationship.get("to_table")
            to_column = relationship.get("to_column")

            source = table_columns.get(from_table, {}).get(from_column)
            target = table_columns.get(to_table, {}).get(to_column)

            if source is None or target is None:
                relationship["validation_status"] = "invalid"
                relationship["validation_reason"] = (
                    "One or both columns do not exist in the metadata."
                )
                validated_relationships.append(relationship)
                continue

            source_type = str(source.get("data_type") or "").lower()
            target_type = str(target.get("data_type") or "").lower()

            relationship["source_data_type"] = source_type
            relationship["target_data_type"] = target_type

            if (
                source_type
                and target_type
                and source_type != target_type
            ):
                relationship["validation_status"] = (
                    "type_mismatch_review_required"
                )
                relationship["validation_reason"] = (
                    "Column data types differ. Verify compatibility "
                    "before considering this relationship."
                )
                validated_relationships.append(relationship)
                continue

            relationship["validation_status"] = (
                "metadata_verified_business_review_required"
            )
            relationship["validation_reason"] = (
                "Both columns exist and their data types are compatible. "
                "The business relationship is not yet confirmed."
            )

            if run_data_checks:
                if connector is None:
                    raise ValueError(
                        "A SQL connector is required when run_data_checks=True."
                    )

                # Identifiers must come from the trusted metadata inventory,
                # never directly from arbitrary user or AI-generated SQL.
                def quote_identifier(value: str) -> str:
                    return "[" + value.replace("]", "]]") + "]"

                source_sql = (
                    f"{quote_identifier(from_table)}."
                    f"{quote_identifier(from_column)}"
                )
                target_sql = (
                    f"{quote_identifier(to_table)}."
                    f"{quote_identifier(to_column)}"
                )

                # These queries are read-only. They assume the table names
                # returned by the inventory are schema-qualified.
                query = f"""
                    SELECT
                        COUNT_BIG(*) AS source_rows,
                        COUNT_BIG(CASE
                            WHEN s.{quote_identifier(from_column)} IS NOT NULL
                            THEN 1
                        END) AS non_null_source_rows,
                        COUNT_BIG(CASE
                            WHEN s.{quote_identifier(from_column)} IS NOT NULL
                             AND t.{quote_identifier(to_column)} IS NOT NULL
                            THEN 1
                        END) AS matched_rows,
                        COUNT_BIG(CASE
                            WHEN s.{quote_identifier(from_column)} IS NOT NULL
                             AND t.{quote_identifier(to_column)} IS NULL
                            THEN 1
                        END) AS unmatched_rows
                    FROM {quote_identifier(from_table)} AS s
                    LEFT JOIN {quote_identifier(to_table)} AS t
                      ON s.{quote_identifier(from_column)}
                       = t.{quote_identifier(to_column)};
                """

                uniqueness_query = f"""
                    SELECT COUNT_BIG(*) AS duplicate_key_groups
                    FROM (
                        SELECT {quote_identifier(to_column)}
                        FROM {quote_identifier(to_table)}
                        WHERE {quote_identifier(to_column)} IS NOT NULL
                        GROUP BY {quote_identifier(to_column)}
                        HAVING COUNT_BIG(*) > 1
                    ) AS duplicate_keys;
                """

                with connector.connect() as connection:
                    cursor = connection.cursor()

                    cursor.execute(query)
                    data_row = cursor.fetchone()

                    cursor.execute(uniqueness_query)
                    uniqueness_row = cursor.fetchone()

                source_rows = int(data_row[0])
                non_null_source_rows = int(data_row[1])
                matched_rows = int(data_row[2])
                unmatched_rows = int(data_row[3])
                duplicate_key_groups = int(uniqueness_row[0])

                relationship["data_profile"] = {
                    "source_rows": source_rows,
                    "non_null_source_rows": non_null_source_rows,
                    "matched_rows": matched_rows,
                    "unmatched_rows": unmatched_rows,
                    "duplicate_target_key_groups": duplicate_key_groups,
                }

                if duplicate_key_groups > 0:
                    relationship["validation_status"] = (
                        "target_key_not_unique_review_required"
                    )
                    relationship["validation_reason"] = (
                        "The target column contains duplicate key groups. "
                        "A many-to-one relationship cannot be assumed."
                    )
                elif unmatched_rows > 0:
                    relationship["validation_status"] = (
                        "unmatched_keys_review_required"
                    )
                    relationship["validation_reason"] = (
                        "Some non-null source keys have no matching target "
                        "key. Review referential integrity."
                    )
                else:
                    relationship["validation_status"] = (
                        "data_validated_business_review_required"
                    )
                    relationship["validation_reason"] = (
                        "No unmatched non-null source keys were found, "
                        "and the target key is unique. Business meaning "
                        "and relationship cardinality still require review."
                    )

            validated_relationships.append(relationship)

        updated_mapping["relationships"] = validated_relationships
        validated_mappings.append(updated_mapping)

    return {
        **semantic_mapping,
        "mappings": validated_mappings,
    }