# HarkenIQ Requirements

Requirement documents live in [`docs/requirements/`](requirements/).

| # | Document | Covers |
|---|---|---|
| 1 | [Platform Architecture](requirements/01-architecture.md) | What HarkenIQ is. Harken Mesh, Harken Site Manager, Harken Central Command — definition, responsibilities, how the intelligence is split, and what must be true for the architecture to be right. |
| 2 | [Market, Comparative Analysis and Build Plan](requirements/02-market-and-build-plan.md) | Evidenced problem, competitive landscape, where the gap actually is, claims retired, market sizing and segments, all slices and categories for use case 1, phasing, risks, validation backlog. |
| 3 | [Credential Validation and Rotation](requirements/03-credential-rotation.md) | Use case 2. Validation-first sequencing, the safe rotation protocol, credential store integration, device class coverage, security requirements. |
| 4 | [Agent Capabilities Roadmap](requirements/04-agent-capabilities-roadmap.md) | All 8 agent ITOM capabilities across R1–R4. Diagnosis, trending, mesh, remediation, config compliance, firmware, asset inventory, OS correlation, credential rotation, audit trail. The complete vision for the sole intelligent operator. |

No technology stack is specified in any of these. Where a capability constrains an eventual technology choice, the requirement is stated so the choice can be made against it.

## Supporting research

- [Premise evidence review](research/premise-evidence.md) — external evidence for and against the original product premise, with sources.
- [Agent feasibility notes](design/agent-feasibility.md) — where software can and cannot be installed across data center device classes.
