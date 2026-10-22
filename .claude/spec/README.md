# Nexora — Specification (Requirements)

Source: `Nexora_Specification_and_Technical_Design.docx`, Part I. Version 0.1 Draft, for Data & AI implementation team review.

> **Development status:** the complete workflow is under development. These documents describe the **target** implementation, not what exists in `app/` today. The target is hosted entirely on Microsoft Fabric, with Rayfin as a Fabric workload ([fabric-hosting.md](../technical-documentation/fabric-hosting.md)).

**Scope:** business prompt intake through semantic-model and Power BI report generation, validation, approval and deployment.

| File | Contents |
|---|---|
| [product-definition.md](product-definition.md) | Executive summary, guiding principles, end-to-end flow, users, in/out of scope |
| [functional-requirements.md](functional-requirements.md) | FR-001 – FR-022 |
| [non-functional-requirements.md](non-functional-requirements.md) | NFR-001 – NFR-010 |
| [business-rules.md](business-rules.md) | Rules that govern approvals and progression |
| [acceptance-scenarios.md](acceptance-scenarios.md) | Given/When/Then scenarios, definition of done for the first vertical slice, review checklist |

The technical design (Part II) is in [`../technical-documentation/`](../technical-documentation/README.md).
