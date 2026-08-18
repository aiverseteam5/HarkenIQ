# HarkenIQ Requirements

Requirement documents live in [`docs/requirements/`](requirements/).

| # | Document | Covers |
|---|---|---|
| 1 | [Platform Architecture](requirements/01-architecture.md) | What HarkenIQ is. Harken Mesh, Harken Site Manager, Harken Central Command — definition, responsibilities, how the intelligence is split, and what must be true for the architecture to be right. |
| 2 | [Market, Comparative Analysis and Build Plan](requirements/02-market-and-build-plan.md) | Evidenced problem, competitive landscape, where the gap actually is, claims retired, market sizing and segments, all slices and categories for use case 1, phasing, risks, validation backlog. |
| 3 | [Credential Validation and Rotation](requirements/03-credential-rotation.md) | Use case 2. Validation-first sequencing, the safe rotation protocol, credential store integration, device class coverage, security requirements. |
| 4 | [Agent Capabilities Roadmap](requirements/04-agent-capabilities-roadmap.md) | All 8 agent ITOM capabilities across R1–R4. Diagnosis, trending, mesh, remediation, config compliance, firmware, asset inventory, OS correlation, credential rotation, audit trail. The complete vision for the sole intelligent operator. |
| 5 | [Redfish API Catalog](requirements/05-redfish-api-catalog.md) | Implementation-ready endpoint reference for the 5 R1 fault types (fan, disk, memory, PSU, thermal) across 4 target devices (Dell iDRAC9/10, HPE iLO5/6). Polling strategy, normalization schemas, fault detection rules, vendor differences, mock simulator requirements. |
| 6 | [Agent Runtime Architecture](requirements/06-agent-runtime-architecture.md) | Process model, privilege model, directory layout, configuration, CLI interface, state persistence (SQLite), logging, peer heartbeat protocol, systemd deployment, packaging, startup/shutdown sequences. Updated with D6-D18 decisions: action pipeline, interactive TUI, HMAC heartbeat, debounce, exit codes. |
| 7 | [Skill YAML Schema](requirements/07-skill-yaml-schema.md) | Skill file format, expression DSL grammar (infix conditions with AND/OR/NOT), verdict outcomes, debounce overrides, action recommendations, trending section, complete examples for all 5 fault types, parser implementation notes. |
| 8 | [Vendor Normalization Schema](requirements/08-vendor-normalization.md) | Unified normalized data model (NormalizedFan, NormalizedDisk, etc.), field-by-field mapping tables (Dell→Common, HPE→Common), HPE SmartStorage compatibility, OEM field preservation, Python dataclass definitions. |
| 9 | [Demo Scenario Script](requirements/09-demo-scenario.md) | Second-by-second timeline for `harken demo` 60-second showcase. Progressive failure cascade: fan trending → disk SMART → peer witness → PSU failure → thermal cascade. TUI state at each stage, acceptance criteria. |
| 10 | [Internal API Specification](requirements/10-internal-api-spec.md) | Python dataclass definitions for all domain objects, module interface contracts with async function signatures, error type hierarchy, end-to-end data flow trace, async task coordination rules. |
| 11 | [Mock Simulator Specification](requirements/11-mock-simulator.md) | Redfish mock server architecture, endpoint routing, fixture file format, fault injection API, state management, session authentication, error simulation, peer simulation for demo. |
| 12 | [Test Plan](requirements/12-test-plan.md) | Test pyramid (unit/integration/e2e), 22 test paths enumerated, test fixtures, performance criteria, debounce regression tests, security tests, CI integration. |
| 13 | [Baseline and Trending Algorithm](requirements/13-baseline-trending.md) | Welford's online algorithm for baselines, linear regression for trending, confidence metric, learning mode, edge cases (discontinuity, counter sensors, oscillation), checkpoint persistence. |

Documents 5-13 specify concrete technology choices and implementation details for R1 (Python 3.11+, asyncio, aiohttp, SQLite, gRPC, systemd, rich TUI).

## Supporting research

- [Premise evidence review](research/premise-evidence.md) — external evidence for and against the original product premise, with sources.
- [Agent feasibility notes](design/agent-feasibility.md) — where software can and cannot be installed across data center device classes.
