# Test fixtures

`create_test_tables.ipynb` is a PySpark notebook that creates small synthetic tables in schema `nexora_test` of the **sandbox** Lakehouse. They exist so the acceptance scenarios in `.claude/spec/acceptance-scenarios.md` can be tested: the `saleslt` sample has no duplicate dimension keys, no many-to-many relationship and no date table.

## How to run

1. Open the sandbox Lakehouse (it must have **schemas enabled**) and import `create_test_tables.ipynb` as a Fabric notebook.
2. Attach the Lakehouse as the notebook's default Lakehouse. It must also hold the `saleslt` sample tables.
3. Run all cells. The last cell prints the fixture checks and ends with `All fixture checks passed.`

The notebook writes only to schema `nexora_test` and never to `saleslt`. Rerunning overwrites every table with identical content. The Lakehouse SQL endpoint is read-only, so the tables are written with Spark (`saveAsTable`, `overwrite`).

## Tables

All `ProductID` values are real: the first 12 distinct IDs in `saleslt.SalesLT_Product`. Everything else is synthetic.

| Table | Columns | Content | Supports scenario |
|---|---|---|---|
| `nexora_test.dim_product_dupes` | `ProductID int, Name string, Category string` | 12 products, 4 of them listed twice (16 rows) | Invalid relationship: a dimension key with duplicates (T-302, T-303, T-504) |
| `nexora_test.dim_promotion` | `PromotionID int, PromotionName string, StartDate date, EndDate date` | 4 promotions with unique IDs | Many-to-many bridge (T-403, T-506) |
| `nexora_test.bridge_product_promotion` | `ProductID int, PromotionID int` | 16 unique `(ProductID, PromotionID)` pairs. 4 products have two promotions, and every promotion covers several products. | Many-to-many bridge |
| `nexora_test.dim_campaign` | `CampaignID int, CampaignName string, ProductCategory string` | 4 campaigns related to products only through the free-text `ProductCategory`. No mapping table exists, on purpose. | Unsupported bridge (T-403) |
| `nexora_test.dim_date` | `Date date, Year int, Quarter string, MonthNumber int, MonthName string` | One row per day for every calendar year that has an `OrderDate` in `saleslt.SalesLT_SalesOrderHeader`. `Date` is unique. | Monthly KPIs and the Calendar hierarchy (T-404, T-502, T-601) |

## Checks

The notebook's last cell asserts: at least 3 duplicated `ProductID` values in `dim_product_dupes`, no duplicate pairs in the bridge, every bridge `ProductID` exists in `saleslt.SalesLT_Product`, and every order date is in `dim_date`.

To list the tables from the project, run `python -m app.test_metadata_inventory nexora_test`.
