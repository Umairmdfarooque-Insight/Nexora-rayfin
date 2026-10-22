"""Bridge-table detection (T-403, FR-008, business rule 4, §15): where does the model need a
mapping between two entities, and is there a mapping we may use?

Nothing is generated and nothing is guessed. For the entities SourceResolution selected:
1. A mapping table is an inventory table, selected for no entity, whose every column joins
   (with medium or high confidence) into a table selected for an entity, and which joins into
   at least two such tables. Evidence is aggregate-only: duplicate pairs and key coverage.
2. A direct many_to_many relationship between two selected tables also needs a bridge. One
   between tables no entity uses is ignored and logged.
3. An entity whose table joins to nothing is isolated: it can't be related to the others.
For each pair or isolated entity, an existing mapping table is proposed first (the one with the
highest coverage), then an approved derivation rule, otherwise `needs_rules`.
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable

from app.contracts.bridge_proposal import BridgeProposal
from app.contracts.metadata_inventory import MetadataInventory
from app.contracts.source_resolution import SourceResolution
from app.discovery.derivation_rules import DerivationRule
from app.discovery.data_profiler import qualified, quote_identifier
from app.discovery.glossary import normalize

logger = logging.getLogger(__name__)

STRONG = ("high", "medium")


@dataclass(frozen=True)
class MappingCandidate:
    table: str  # the mapping table, "schema.table"
    a_entity: str
    a_table: str  # table selected for entity a
    a_key: str  # key column in a_table
    a_column: str  # column of the mapping table that joins to it
    b_entity: str
    b_table: str
    b_key: str
    b_column: str


Measure = Callable[[MappingCandidate], dict[str, Any]]


def _percent(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def measure_mapping(connection: Any, inventory: MetadataInventory, c: MappingCandidate) -> dict[str, Any]:
    """Duplicate pairs and key coverage of a mapping table, over the SQL endpoint. Read-only
    aggregates; identifiers are bracket-quoted from the inventory."""

    def split(name: str) -> tuple[str, str]:
        schema, _, table = name.partition(".")
        return schema, table

    mapping = qualified(*split(c.table))
    a_col, b_col = quote_identifier(c.a_column), quote_identifier(c.b_column)
    queries = [
        "SELECT COUNT_BIG(*) FROM (SELECT "
        f"{a_col}, {b_col} FROM {mapping} WHERE {a_col} IS NOT NULL AND {b_col} IS NOT NULL "
        f"GROUP BY {a_col}, {b_col} HAVING COUNT_BIG(*) > 1) AS d"
    ]
    for table, key, column in ((c.a_table, c.a_key, a_col), (c.b_table, c.b_key, b_col)):
        target, k = qualified(*split(table)), quote_identifier(key)
        queries.append(
            "SELECT COUNT_BIG(*), SUM(CASE WHEN m.[k] IS NOT NULL THEN 1 ELSE 0 END) "
            f"FROM (SELECT DISTINCT {k} AS [k] FROM {target} WHERE {k} IS NOT NULL) AS t "
            f"LEFT JOIN (SELECT DISTINCT {column} AS [k] FROM {mapping} WHERE {column} IS NOT NULL) AS m "
            "ON t.[k] = m.[k]"
        )
    cursor = connection.cursor()
    cursor.execute(queries[0])
    duplicates = int(cursor.fetchone()[0] or 0)
    coverage = []
    for sql in queries[1:]:
        cursor.execute(sql)
        total, matched = cursor.fetchone()
        coverage.append(_percent(int(matched or 0), int(total or 0)))
    return {
        "duplicate_pairs": duplicates,
        "coverage_a_pct": coverage[0],
        "coverage_b_pct": coverage[1],
        "queries": queries,
    }


def find_mapping_tables(
    inventory: MetadataInventory, entity_of: dict[str, str], order: dict[str, int]
) -> dict[tuple[str, str], list[MappingCandidate]]:
    """Mapping-table candidates per entity pair (the pair is in contract order)."""
    strong = [e for e in inventory.relationship_evidence if e.status == "candidate" and e.confidence in STRONG]
    found: dict[tuple[str, str], list[MappingCandidate]] = {}
    for table in inventory.tables:
        key = f"{table.schema_name}.{table.table_name}"
        if key.lower() in entity_of:
            continue
        outgoing = [e for e in strong if e.from_.table.lower() == key.lower()]
        if not table.columns or not {c.name for c in table.columns} <= {e.from_.column for e in outgoing}:
            continue  # not a pure mapping table: some column is not a join column
        targets: dict[str, Any] = {}
        for e in sorted(outgoing, key=lambda e: (e.to.table.lower(), e.from_.column.lower())):
            if e.to.table.lower() in entity_of:
                targets.setdefault(e.to.table.lower(), e)
        ordered = sorted(targets.values(), key=lambda e: (order[entity_of[e.to.table.lower()]], e.to.table.lower()))
        for i, x in enumerate(ordered):
            for y in ordered[i + 1 :]:
                a, b = entity_of[x.to.table.lower()], entity_of[y.to.table.lower()]
                if a == b or x.from_.column == y.from_.column:
                    continue
                found.setdefault((a, b), []).append(
                    MappingCandidate(key, a, x.to.table, x.to.column, x.from_.column, b, y.to.table, y.to.column, y.from_.column)
                )
    return found


def _names(entities: list[str]) -> str:
    return " and ".join(entities)


def detect(
    inventory: MetadataInventory,
    resolution: SourceResolution,
    rules: list[DerivationRule],
    measure: Measure,
) -> list[BridgeProposal]:
    """Proposals in a deterministic order, with empty ids (the stage numbers them)."""
    selected = [e for e in resolution.entities if e.selected is not None]
    entity_of = {e.selected.table.lower(): e.entity for e in selected}
    order = {e.entity: i for i, e in enumerate(selected)}
    mapping = find_mapping_tables(inventory, entity_of, order)

    pairs = set(mapping)
    for e in inventory.relationship_evidence:
        if e.status != "candidate" or e.cardinality != "many_to_many":
            continue
        a, b = entity_of.get(e.from_.table.lower()), entity_of.get(e.to.table.lower())
        if a and b and a != b:
            pairs.add(tuple(sorted((a, b), key=order.__getitem__)))
        else:
            logger.info(
                "Ignored a many-to-many between %s and %s: no entity of the contract needs it",
                e.from_.table, e.to.table,
            )

    proposals: list[BridgeProposal] = []
    for pair in sorted(pairs, key=lambda p: (order[p[0]], order[p[1]])):
        a, b = pair
        measured = sorted(
            ((c, measure(c)) for c in mapping.get(pair, [])),
            key=lambda cm: (-(cm[1]["coverage_a_pct"] + cm[1]["coverage_b_pct"]), cm[0].table.lower()),
        )
        if measured:
            best, evidence = measured[0]
            issues = []
            dedup = next((r.id for r in rules if r.deduplicate and r.covers(a, b)), None)
            if evidence["duplicate_pairs"]:
                issues.append(f"duplicate pairs: {evidence['duplicate_pairs']}")
            for entity, pct in ((a, evidence["coverage_a_pct"]), (b, evidence["coverage_b_pct"])):
                if pct == 0:
                    issues.append(f"no {entity} key appears in the mapping table")
            issues += [
                f"other mapping table candidate: {c.table} (coverage {m['coverage_a_pct']}% / {m['coverage_b_pct']}%)"
                for c, m in measured[1:]
            ]
            proposals.append(
                BridgeProposal(
                    id="",
                    entities=[a, b],
                    situation="existing_table",
                    table=best.table,
                    deduplication_rule=dedup,
                    evidence={k: evidence[k] for k in ("duplicate_pairs", "coverage_a_pct", "coverage_b_pct", "queries")},
                    issues=issues,
                )
            )
            continue
        rule = next((r for r in rules if not r.deduplicate and r.covers(a, b)), None)
        if rule is not None:
            proposals.append(BridgeProposal(id="", entities=[a, b], situation="derivation_rule", derivation_rule=rule.id))
        else:
            proposals.append(_needs_rules([a, b]))

    touched = {
        t.lower()
        for e in inventory.relationship_evidence
        if e.status == "candidate" and e.confidence in STRONG
        for t in (e.from_.table, e.to.table)
    }
    in_pair = {entity for pair in pairs for entity in pair}
    covered: set[str] = set()
    for e in selected:
        if e.entity in in_pair or e.entity in covered or e.selected.table.lower() in touched:
            continue
        others = [o.entity for o in selected if o.entity != e.entity]
        if not others:
            continue
        rule = next((r for r in rules if not r.deduplicate and any(r.covers(e.entity, o) for o in others)), None)
        if rule is not None:
            partner = next(o for o in others if rule.covers(e.entity, o))
            proposals.append(
                BridgeProposal(id="", entities=[partner, e.entity], situation="derivation_rule", derivation_rule=rule.id)
            )
        else:
            proposals.append(_needs_rules([*others, e.entity]))
            covered.update(others)
        covered.add(e.entity)
    return proposals


def _needs_rules(entities: list[str]) -> BridgeProposal:
    return BridgeProposal(
        id="",
        entities=entities,
        situation="needs_rules",
        issues=[
            f"mapping rules required: no mapping table links {_names(entities)}, "
            "and no approved derivation rule covers them"
        ],
    )


def entity_key(entities: list[str]) -> tuple[str, ...]:
    return tuple(sorted(normalize(e) for e in entities))
