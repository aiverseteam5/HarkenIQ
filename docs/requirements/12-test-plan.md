# Document 12: Test Plan

**Purpose:** Complete test strategy, test matrix, and acceptance criteria for HarkenIQ R1.
**Scope:** Unit, integration, and end-to-end tests covering all R1 features.
**Status:** Draft.

---

## 1. Test Strategy

### 1.1 Test Pyramid

| Layer | Coverage Target | Count | Purpose |
|-------|----------------|-------|---------|
| Unit tests | 80%+ line coverage on core modules | ~60% of tests | Verify individual module behavior |
| Integration tests | All 22 test paths pass | ~30% of tests | Verify module interactions against mock |
| End-to-end tests | All CLI commands and demo pass | ~10% of tests | Verify user-facing behavior |

### 1.2 Test Framework

| Tool | Purpose |
|------|---------|
| `pytest` | Test runner |
| `pytest-asyncio` | Async test support |
| `pytest-cov` | Coverage reporting |
| Mock simulator | Redfish API responses |
| `unittest.mock` | Internal module mocking |

### 1.3 Test Execution

```bash
# Run all tests
pytest tests/

# Run by layer
pytest tests/unit/
pytest tests/integration/
pytest tests/e2e/

# Run with coverage
pytest --cov=harkeniq --cov-report=html tests/

# Run a single test path
pytest tests/integration/test_fan_dell.py
```

---

## 2. Unit Tests

### 2.1 Module Coverage

| Module | Test File | Key Tests |
|--------|-----------|-----------|
| `redfish.normalize` | `test_normalize.py` | Normalize Dell fan, HPE fan, Dell disk, HPE disk (SmartStorage + standard), Dell memory, HPE memory, Dell PSU, HPE PSU, Dell thermal, HPE thermal, health rollup |
| `redfish.discovery` | `test_discovery.py` | Auto-detect Dell via Oem.Dell, auto-detect HPE via Oem.Hpe, detect iDRAC9/10, detect iLO5/6, fallback to config |
| `redfish.client` | `test_client.py` | Session creation, session renewal, GET request, error handling (401, 403, 404, 500, 503), timeout, retry with backoff |
| `skills.expression` | `test_expression.py` | Parse simple comparison, parse AND, parse OR, parse NOT, operator precedence, field reference, string comparison, numeric comparison, None handling, type mismatch, max depth, max length |
| `skills.engine` | `test_engine.py` | Single rule match, multiple rules (highest severity wins), no match (default verdict), debounce N-of-M, evidence generation, action recommendation |
| `skills.loader` | `test_loader.py` | Load valid YAML, validation errors (missing name, bad target, bad condition, unknown field, unknown action), duplicate detection |
| `skills.trending` | `test_trending.py` | Linear regression slope, R² calculation, time-to-threshold projection, direction filter (declining only), insufficient samples, flat trend, discontinuity detection |
| `heartbeat.protocol` | `test_heartbeat.py` | Packet serialization, HMAC generation, HMAC verification, HMAC rejection (wrong key), packet size limit |
| `heartbeat.tracker` | `test_tracker.py` | Peer alive detection, peer unresponsive (3 misses), peer recovery, pre-failure evidence retention, unknown → alive transition |
| `state.checkpoint` | `test_checkpoint.py` | Write checkpoint, read checkpoint, restore baselines, restore peer table, corrupt DB recovery, WAL mode verification |
| `state.machine` | `test_machine.py` | Valid transitions, invalid transitions, crash recovery (ACTING → UNKNOWN outcome), BOOTING → OBSERVING |
| `security.credentials` | `test_credentials.py` | Encrypt credentials, decrypt credentials, wrong key rejection, corrupted file handling |
| `reporting.console` | `test_console.py` | Render healthy state, render with warnings, render with pending actions, render peer unresponsive |

### 2.2 Normalization Unit Tests (Detail)

Each normalization test: input = raw Redfish JSON dict, output = normalized dataclass.

| Test | Input Fixture | Expected Output |
|------|--------------|-----------------|
| `test_normalize_dell_fan` | Dell R750 `/Chassis/.../Thermal` Fans array | NormalizedFan with speed_rpm, health, thresholds |
| `test_normalize_hpe_fan` | HPE DL360 `/Chassis/1/Thermal` Fans array | Same fields, different source paths |
| `test_normalize_dell_disk` | Dell R750 `/Systems/.../Drives/...` | NormalizedDisk with life_left_pct, raid_status |
| `test_normalize_hpe_disk_standard` | HPE iLO6 `/Systems/1/Storage/.../Drives/...` | NormalizedDisk without SmartStorage fields |
| `test_normalize_hpe_disk_smartstorage` | HPE iLO5 `/Systems/1/SmartStorage/.../DiskDrives/...` | NormalizedDisk with inverted SSDEnduranceUtilizationPercentage |
| `test_normalize_dell_memory` | Dell R750 `/Systems/.../Memory/DIMM.Socket.A1` | NormalizedMemory with all ECC fields |
| `test_normalize_dell_memory_metrics` | Dell R750 `.../MemoryMetrics` | ECC error counts |
| `test_normalize_hpe_memory` | HPE DL360 `/Systems/1/Memory/proc1dimm1` | Same normalized output |
| `test_normalize_dell_psu` | Dell R750 `/Chassis/.../Power` PowerSupplies | NormalizedPSU with redundancy |
| `test_normalize_hpe_psu` | HPE DL360 `/Chassis/1/Power` PowerSupplies | Same normalized output |
| `test_normalize_dell_thermal` | Dell R750 `/Chassis/.../Thermal` Temperatures | NormalizedThermal with all thresholds |
| `test_normalize_hpe_thermal` | HPE DL360 `/Chassis/1/Thermal` Temperatures | Same normalized output |
| `test_normalize_missing_field` | Redfish response with missing optional field | Normalized with None for missing fields |
| `test_normalize_malformed_response` | Invalid JSON structure | Returns health=UNKNOWN with error |

### 2.3 Expression Parser Unit Tests

| Test | Input Expression | Expected AST / Result |
|------|-----------------|----------------------|
| `test_simple_comparison` | `"health == 'Critical'"` | Comparison(field="health", op="==", value="Critical") |
| `test_numeric_comparison` | `"speed_rpm < 2000"` | Comparison(field="speed_rpm", op="<", value=2000) |
| `test_and_expression` | `"health == 'Critical' AND state == 'Enabled'"` | BooleanOp(op="AND", left=..., right=...) |
| `test_or_expression` | `"health == 'Critical' OR health == 'Warning'"` | BooleanOp(op="OR", ...) |
| `test_not_expression` | `"NOT state == 'Absent'"` | NotOp(operand=...) |
| `test_field_reference_value` | `"speed_rpm < threshold_low_critical"` | Right side resolved from context |
| `test_none_field_returns_false` | `"missing_field > 100"` | False (None never triggers) |
| `test_case_insensitive_keywords` | `"health == 'OK' and state == 'Enabled'"` | Parses correctly |
| `test_max_depth_exceeded` | 21 levels of nested NOT | SkillParseError |
| `test_max_length_exceeded` | 1001 character expression | SkillParseError |
| `test_type_mismatch` | `"speed_rpm == 'fast'"` (number vs string) | False with warning log |
| `test_eval_true` | `"health == 'Critical'"` with context `{"health": "Critical"}` | True |
| `test_eval_false` | `"health == 'Critical'"` with context `{"health": "OK"}` | False |

---

## 3. Integration Tests

### 3.1 The 22 Test Paths

Each integration test: start mock simulator → poll → normalize → evaluate skills → verify verdict.

#### Fault Detection Paths (10)

| # | Fault Type | Vendor | Test |
|---|-----------|--------|------|
| 1 | Fan failure | Dell | Inject fan Critical → verify CRITICAL verdict |
| 2 | Fan failure | HPE | Inject fan Critical → verify CRITICAL verdict |
| 3 | Disk SMART | Dell | Inject FailurePredicted + low SSD life → verify WARNING |
| 4 | Disk SMART | HPE | Inject SSD life low (via SmartStorage for iLO5) → verify WARNING |
| 5 | Memory ECC | Dell | Inject uncorrectable ECC alarm → verify CRITICAL |
| 6 | Memory ECC | HPE | Inject uncorrectable ECC alarm → verify CRITICAL |
| 7 | PSU failure | Dell | Inject PSU Absent → verify CRITICAL + redundancy WARNING |
| 8 | PSU failure | HPE | Inject PSU Absent → verify CRITICAL + redundancy WARNING |
| 9 | Thermal | Dell | Inject reading above UpperThresholdCritical → verify CRITICAL |
| 10 | Thermal | HPE | Inject reading above UpperThresholdCritical → verify CRITICAL |

#### Fault Recovery Paths (10)

| # | Fault Type | Vendor | Test |
|---|-----------|--------|------|
| 11 | Fan recovery | Dell | Clear fan fault → verify recovery to HEALTHY (3/3 debounce) |
| 12 | Fan recovery | HPE | Same |
| 13 | Disk recovery | Dell | Clear SMART alert → verify WARNING → HEALTHY |
| 14 | Disk recovery | HPE | Same |
| 15 | Memory recovery | Dell | Clear ECC alarm → verify CRITICAL → HEALTHY |
| 16 | Memory recovery | HPE | Same |
| 17 | PSU recovery | Dell | Re-insert PSU → verify CRITICAL → HEALTHY + redundancy OK |
| 18 | PSU recovery | HPE | Same |
| 19 | Thermal recovery | Dell | Drop temp below threshold → verify CRITICAL → HEALTHY |
| 20 | Thermal recovery | HPE | Same |

#### Cross-Cutting Paths (2)

| # | Feature | Test |
|---|---------|------|
| 21 | Peer heartbeat | Start 2 agents → one stops heartbeat → verify unresponsive detection within 30s |
| 22 | Trending prediction | Inject gradually declining fan RPM → verify TRENDING verdict with slope and time-to-threshold |

### 3.2 Integration Test Structure

```python
# Example: tests/integration/test_fan_dell.py

@pytest.mark.asyncio
async def test_fan_failure_dell(mock_simulator):
    """Test path #1: Dell fan failure detection."""
    # Setup: start mock simulator with healthy Dell R750
    sim = mock_simulator("dell-r750")

    # Create agent with mock BMC
    agent = create_test_agent(bmc_url=sim.url)

    # Poll and verify healthy
    verdicts = await agent.poll_and_evaluate()
    assert all(v.verdict == "HEALTHY" for v in verdicts if v.sensor_type == "fan")

    # Inject fan failure
    sim.inject_fault("fan", target="Fan1A", health="Critical", speed_rpm=0)

    # Poll and verify (debounce: 2 of 3 for critical)
    verdicts = await agent.poll_and_evaluate()  # 1 of 3
    verdicts = await agent.poll_and_evaluate()  # 2 of 3 → CRITICAL

    fan_verdict = next(v for v in verdicts if "Fan1A" in v.sensor_id)
    assert fan_verdict.verdict == "CRITICAL"
    assert "Fan1A" in fan_verdict.message
    assert fan_verdict.evidence.fields["health"] == "Critical"
```

### 3.3 Mock Simulator Integration

Tests use the mock simulator (Doc 11) as a fixture:

```python
@pytest.fixture
async def mock_simulator():
    """Start a mock Redfish simulator for integration tests."""
    sim = MockRedfishSimulator()
    await sim.start(port=0)  # Random available port
    yield sim
    await sim.stop()
```

---

## 4. End-to-End Tests

### 4.1 CLI Tests

| Test | Command | Verification |
|------|---------|-------------|
| `test_diagnose_healthy` | `harken diagnose --bmc-ip localhost:8443` | Exit code 0, output contains "OK" for all subsystems |
| `test_diagnose_warning` | Inject disk warning, then `harken diagnose` | Exit code 1, output contains "WARNING" |
| `test_diagnose_critical` | Inject PSU failure, then `harken diagnose` | Exit code 2, output contains "CRITICAL" |
| `test_diagnose_json` | `harken diagnose --json` | Valid JSON, matches expected schema |
| `test_diagnose_bmc_unreachable` | `harken diagnose --bmc-ip 192.168.255.255` | Exit code 3, error message about unreachable |
| `test_config_validate` | `harken config validate` with valid config | Exit code 0 |
| `test_config_validate_bad` | `harken config validate` with invalid YAML | Exit code 4 |
| `test_skills_validate` | `harken skills validate` with all default skills | Exit code 0 |
| `test_skills_validate_bad` | Add malformed skill file | Exit code 4, error message identifies the file |
| `test_bmc_detect` | `harken bmc detect` against mock | Prints "Dell PowerEdge R750 (iDRAC9)" |
| `test_version` | `harken version` | Prints version string |

### 4.2 Demo Test

| Test | Command | Verification |
|------|---------|-------------|
| `test_demo_completes` | `harken demo --mock --speed 10` | Exit code 0, completes in <10s |
| `test_demo_all_verdicts` | `harken demo --mock --speed 10` | Output contains CRITICAL, WARNING, TRENDING |
| `test_demo_peer_down` | `harken demo --mock --speed 10` | Output contains "UNRESPONSIVE" |
| `test_demo_actions_proposed` | `harken demo --mock --speed 10` | Output contains "PENDING ACTIONS" |

### 4.3 Agent Lifecycle Test

```python
@pytest.mark.asyncio
async def test_agent_lifecycle():
    """Start agent, poll, checkpoint, restart, verify state restored."""
    # Start agent
    agent = create_test_agent()
    await agent.start()

    # Wait for a few polls
    await asyncio.sleep(5)

    # Verify checkpoint was written
    assert Path("/tmp/test-harkeniq/checkpoint.db").exists()

    # Simulate crash (stop without graceful shutdown)
    agent.force_kill()

    # Restart
    agent2 = create_test_agent()
    await agent2.start()

    # Verify baselines restored
    assert agent2.baselines is not None
    assert len(agent2.baselines) > 0

    # Verify peer table restored
    assert len(agent2.peers) == 2

    await agent2.stop()
```

---

## 5. Performance Tests

| Criterion | Target | Test Method |
|-----------|--------|-------------|
| Startup to OBSERVING | < 30 seconds | Time from process start to first poll completion |
| Single poll cycle | < 5 seconds | Time from poll start to verdicts produced |
| Memory steady state | < 256 MB | `psutil.Process().memory_info().rss` after 100 polls |
| CPU steady state | < 10% of 1 core | Average CPU over 60 seconds of polling |
| Checkpoint write | < 500 ms | Time to write checkpoint.db |
| Checkpoint restore | < 2 seconds | Time to load checkpoint on restart |
| Expression parse | < 1 ms per expression | Time to parse + evaluate one skill condition |
| Heartbeat round-trip | < 10 ms on localhost | UDP send → receive on loopback |

---

## 6. Debounce Regression Tests

| Test | Setup | Expected |
|------|-------|----------|
| `test_critical_2_of_3` | 1 critical, 1 healthy, 1 critical | Still HEALTHY (only 1 consecutive) |
| `test_critical_2_of_3_consecutive` | 2 critical consecutive | CRITICAL (2 of 3 met) |
| `test_warning_3_of_5` | 3 warnings in 5 polls (non-consecutive) | WARNING |
| `test_warning_2_of_5` | 2 warnings in 5 polls | Still HEALTHY |
| `test_recovery_3_of_3` | 3 consecutive HEALTHY after CRITICAL | HEALTHY (recovered) |
| `test_recovery_2_of_3` | 2 HEALTHY, 1 WARNING after CRITICAL | Still CRITICAL (need 3 consecutive) |
| `test_trending_no_debounce` | 1 TRENDING verdict | TRENDING immediately (no debounce) |

---

## 7. Security Tests

| Test | Input | Expected |
|------|-------|----------|
| `test_hmac_valid` | Heartbeat with correct HMAC | Packet accepted |
| `test_hmac_invalid` | Heartbeat with wrong HMAC | Packet rejected, warning logged |
| `test_hmac_missing` | Heartbeat without HMAC field | Packet rejected |
| `test_credential_encryption` | Encrypt + decrypt cycle | Plaintext matches |
| `test_credential_wrong_key` | Decrypt with wrong machine-id | Decryption fails gracefully |
| `test_expression_no_eval` | Skill with Python code injection attempt | SkillParseError (not executed) |
| `test_expression_max_depth` | Deeply nested expression | SkillParseError (max depth 20) |
| `test_skill_no_file_access` | Skill condition referencing file path | Field not found, evaluates to false |

---

## 8. Test Fixtures

### 8.1 Fixture Directory Structure

```
tests/
├── fixtures/
│   ├── dell_r750/
│   │   ├── service_root.json
│   │   ├── manager.json
│   │   ├── system.json
│   │   ├── thermal.json
│   │   ├── thermal_fan_failure.json
│   │   ├── thermal_hot.json
│   │   ├── power.json
│   │   ├── power_psu_absent.json
│   │   ├── storage_collection.json
│   │   ├── drive_healthy.json
│   │   ├── drive_smart_alert.json
│   │   ├── memory_collection.json
│   │   ├── memory_dimm_healthy.json
│   │   ├── memory_dimm_ecc.json
│   │   ├── memory_metrics_healthy.json
│   │   ├── memory_metrics_ecc.json
│   │   └── sel_entries.json
│   ├── dell_r760/
│   │   └── ... (same structure)
│   ├── hpe_dl360_gen10/
│   │   ├── ... (same structure)
│   │   ├── smartstorage_controllers.json
│   │   └── smartstorage_drives.json
│   └── hpe_dl380_gen11/
│       └── ... (same structure, no SmartStorage)
├── skills/
│   ├── fan-health.yaml
│   ├── disk-health.yaml
│   ├── memory-health.yaml
│   ├── psu-health.yaml
│   └── thermal-health.yaml
└── configs/
    ├── valid_config.yaml
    └── invalid_config.yaml
```

### 8.2 Fixture Naming Convention

- `{endpoint}_healthy.json` -- normal/healthy state response
- `{endpoint}_{fault}.json` -- response with a specific fault injected
- Fixtures are static JSON captured from documentation (Week 1: replace with real hardware captures)

---

## 9. CI Integration

```yaml
# .github/workflows/test.yml
name: Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -e ".[dev]"
      - run: pytest --cov=harkeniq --cov-report=xml tests/
      - run: pytest tests/e2e/test_demo.py  # Demo must pass
```

### 9.1 CI Pass Criteria

| Gate | Requirement |
|------|-------------|
| Unit tests | All pass |
| Integration tests | All 22 paths pass |
| E2E tests | Demo completes, diagnose works |
| Coverage | >= 80% on core modules |
| No regressions | No previously passing test fails |
