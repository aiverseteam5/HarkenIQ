# Document 7: Skill YAML Schema Specification

**Purpose:** Formal definition of the skill file format, expression DSL grammar, and evaluation behavior.
**Scope:** R1 skills for 5 fault types (fan, disk, memory, PSU, thermal) with expression-based conditions.
**Status:** Draft.

---

## 1. Overview

Skills are YAML files that define fault detection rules. Each skill targets one sensor type (fan, disk, memory, PSU, thermal) and contains a list of rules evaluated against normalized telemetry data. Skills are vendor-agnostic -- they reference normalized field names from the vendor normalization layer (Doc 08), never raw Redfish properties.

Skills are loaded from `/etc/harkeniq/skills/` at agent startup and re-evaluated after each sensor poll.

---

## 2. Skill File Schema

```yaml
# Required fields
name: string              # Unique skill identifier (e.g., "fan-health")
version: integer           # Schema version (currently 1)
target: string             # Sensor type: fan | disk | memory | psu | thermal
description: string        # Human-readable description

# Rule evaluation
rules:                     # Ordered list of detection rules (first match wins per severity)
  - condition: string      # Expression DSL condition
    verdict: string        # CRITICAL | WARNING | HEALTHY
    message: string        # Message template with {field} substitution
    debounce:              # Optional: override global debounce for this rule
      count: integer       # N (of M)
      window: integer      # M (consecutive polls)
    action:                # Optional: recommended action (R1 allow-list only)
      type: string         # IDENTIFY_LED | COLLECT_DIAGNOSTICS | FAN_RESET
      params: object       # Action-specific parameters

# Trending detection (optional)
trending:
  - field: string          # Normalized field name to track
    direction: string      # declining | rising
    verdict: string        # TRENDING (always)
    message: string        # Message template with {field}, {rate}, {time_to_threshold}
    threshold_field: string # Which threshold to project toward (optional)

# Default verdict when no rule matches
default_verdict: HEALTHY   # Applied when all conditions are false
```

---

## 3. Expression DSL Grammar

### 3.1 Formal Grammar (BNF)

```
expression    := or_expr
or_expr       := and_expr ( 'OR' and_expr )*
and_expr      := not_expr ( 'AND' not_expr )*
not_expr      := 'NOT' not_expr | comparison
comparison    := field_ref OPERATOR value
field_ref     := IDENTIFIER ( '.' IDENTIFIER )*
OPERATOR      := '==' | '!=' | '<' | '>' | '<=' | '>='
value         := NUMBER | STRING | BOOLEAN | FIELD_REF
NUMBER        := integer or float literal
STRING        := single-quoted string literal (e.g., 'Critical')
BOOLEAN       := true | false
IDENTIFIER    := [a-zA-Z_][a-zA-Z0-9_]*
```

### 3.2 Operators

| Operator | Meaning | Example |
|----------|---------|---------|
| `==` | Equal | `health == 'Critical'` |
| `!=` | Not equal | `health != 'OK'` |
| `<` | Less than | `speed_rpm < 2000` |
| `>` | Greater than | `reading_c > 47` |
| `<=` | Less than or equal | `life_left_pct <= 10` |
| `>=` | Greater than or equal | `ecc_correctable_lifetime >= 100` |
| `AND` | Logical and | `health == 'Critical' AND state == 'Enabled'` |
| `OR` | Logical or | `health == 'Critical' OR health == 'Warning'` |
| `NOT` | Logical negation | `NOT state == 'Absent'` |

### 3.3 Operator Precedence (highest to lowest)

1. `NOT`
2. Comparison operators (`==`, `!=`, `<`, `>`, `<=`, `>=`)
3. `AND`
4. `OR`

Parentheses are **not supported** in R1. Use rule ordering to handle complex logic.

### 3.4 Field References

Field names correspond to the normalized sensor model fields (Doc 08):

**Fan fields:** `name`, `speed_rpm`, `speed_pct`, `health`, `state`, `threshold_low_critical`, `redundancy_health`, `location`

**Disk fields:** `name`, `serial`, `media_type`, `protocol`, `capacity_bytes`, `health`, `life_left_pct`, `smart_alert`, `raid_status`, `temperature_c`, `slot`

**Memory fields:** `name`, `capacity_mib`, `type`, `speed_mhz`, `health`, `state`, `socket`, `channel`, `slot`, `ecc_correctable_lifetime`, `ecc_uncorrectable_lifetime`, `ecc_correctable_current`, `alarm_ecc_correctable`, `alarm_ecc_uncorrectable`, `alarm_temperature`

**PSU fields:** `name`, `member_id`, `type`, `capacity_watts`, `output_watts`, `input_voltage`, `health`, `state`, `model`, `serial`, `redundancy_health`, `redundancy_mode`

**Thermal fields:** `name`, `reading_c`, `health`, `threshold_warning`, `threshold_critical`, `threshold_fatal`, `threshold_cold_warning`, `threshold_cold_critical`, `context`

**Baseline fields** (available when confidence >= 0.5):
- `baseline_mean` -- rolling mean of the primary sensor value
- `baseline_stddev` -- rolling standard deviation
- `deviation` -- z-score: (current - mean) / stddev

### 3.5 Special Field: Threshold References

Some conditions compare a sensor value against its own threshold (which varies per device). Use the threshold field name directly:

```yaml
condition: "speed_rpm < threshold_low_critical"
```

The expression evaluator resolves `threshold_low_critical` to the actual threshold value from the normalized sensor data.

### 3.6 Type Coercion Rules

| Left Type | Operator | Right Type | Behavior |
|-----------|----------|------------|----------|
| number | `<`, `>`, `<=`, `>=` | number | Numeric comparison |
| string | `==`, `!=` | string | Case-sensitive string comparison |
| boolean | `==`, `!=` | boolean | Boolean comparison |
| None | any | any | Condition evaluates to `false` (missing data never triggers a verdict) |
| number | `==`, `!=` | string | Error: type mismatch, condition evaluates to `false` with warning log |

---

## 4. Rule Evaluation Semantics

### 4.1 Evaluation Order

1. Rules are evaluated **in order** (top to bottom) within a skill
2. **All matching rules produce verdicts** -- rules are not short-circuited
3. The **highest severity verdict wins** for the final sensor verdict:
   - CRITICAL > WARNING > TRENDING > HEALTHY > UNKNOWN
4. If no rule matches, the `default_verdict` applies (default: HEALTHY)

### 4.2 Per-Sensor Evaluation

Skills target a sensor type (e.g., `target: fan`). The agent evaluates the skill against **every sensor of that type**. For example, if the server has 8 fans, the fan-health skill runs 8 times (once per fan).

### 4.3 Debounce

Each verdict change is subject to debounce (Doc 13 for details):

| Transition | Default Debounce | Behavior |
|-----------|------------------|----------|
| HEALTHY → WARNING | 3 of 5 polls | 3 warning verdicts in last 5 polls |
| HEALTHY → CRITICAL | 2 of 3 polls | 2 critical verdicts in last 3 polls |
| WARNING → CRITICAL | 2 of 3 polls | Same as above |
| Any → HEALTHY (recovery) | 3 of 3 polls | 3 consecutive healthy verdicts |
| Any → TRENDING | No debounce | Trending is gradual, not noisy |

Per-rule debounce overrides are supported:

```yaml
rules:
  - condition: "health == 'Critical'"
    verdict: CRITICAL
    message: "Fan {name} has failed"
    debounce:
      count: 1        # No debounce -- immediate on BMC health change
      window: 1
```

### 4.4 Evidence

Every verdict includes evidence -- the sensor readings that triggered it:

```python
Evidence(
    sensor_id="fan:System Board Fan1A",
    skill_name="fan-health",
    rule_index=0,
    condition="health == 'Critical' AND state == 'Enabled'",
    fields={
        "health": "Critical",
        "state": "Enabled",
        "speed_rpm": 0,
        "name": "System Board Fan1A"
    },
    timestamp="2026-09-15T14:30:00Z",
    baseline_confidence=1.0
)
```

---

## 5. Action Recommendations

Rules can include an optional `action` field recommending a response. In R1, actions require human approval via the TUI queue (D16). Only R1 allow-list actions are permitted (D17).

### 5.1 R1 Action Allow-List

| Action Type | Redfish Operation | Risk | Reversible |
|-------------|------------------|------|------------|
| `IDENTIFY_LED` | `PATCH IndicatorLED = "Blinking"` | None | Yes (set to "Off") |
| `COLLECT_DIAGNOSTICS` | Dell: `POST DellLCService.ExportSystemConfiguration`; HPE: download AHS log | None (read-only) | N/A |
| `FAN_RESET` | `PATCH ThermalProfile = "Default"` | Low | Yes (set back to previous) |

### 5.2 Action Schema

```yaml
action:
  type: IDENTIFY_LED          # Action type from allow-list
  params:
    target: "{name}"          # Which component (supports {field} substitution)
    reason: "{message}"       # Why this action is recommended
```

### 5.3 Action Lifecycle

1. Skill evaluation produces a verdict with an action recommendation
2. Action enters the pending queue (AWAITING_AUTH state)
3. Operator reviews in TUI and approves or denies
4. If approved: agent executes the Redfish operation (ACTING state)
5. Agent verifies the result (e.g., LED is now blinking)
6. Action outcome logged to audit trail (REPORTING state)
7. If denied: action is logged as "denied" and removed from queue

---

## 6. Trending Section

The `trending` section defines predictive trend detection independent of threshold rules.

```yaml
trending:
  - field: speed_rpm                    # Which field to track
    direction: declining                # declining or rising
    verdict: TRENDING                   # Always TRENDING
    message: "Fan {name} declining at {rate} RPM/hr, {time_to_threshold}"
    threshold_field: threshold_low_critical  # Project toward this threshold
```

### 6.1 Trending Evaluation

- Runs only when baseline confidence >= 1.0 (Doc 13)
- Uses linear regression on the ring buffer for the specified field
- Produces a TRENDING verdict when:
  - |slope| > slope_threshold (configurable)
  - R² > 0.5 (data fits a linear trend)
  - Trend direction matches the specified direction
  - Projected time-to-threshold is between 0 and 90 days

### 6.2 Message Template Fields for Trending

| Template | Value | Example |
|----------|-------|---------|
| `{name}` | Sensor name | "System Board Fan1A" |
| `{rate}` | Slope with units per hour | "-8.5 RPM/hr" |
| `{time_to_threshold}` | Projected time | "critical in 42 days" |
| `{field}` | Current value of the tracked field | "9200" |
| `{threshold}` | Value of threshold_field | "480" |

---

## 7. Complete Skill Examples

### 7.1 Fan Health Skill

```yaml
name: fan-health
version: 1
target: fan
description: Detect fan failures, degradation, and predictive RPM decline

rules:
  - condition: "health == 'Critical' AND state == 'Enabled'"
    verdict: CRITICAL
    message: "Fan {name} has failed (health critical, still present)"
    debounce:
      count: 1
      window: 1
    action:
      type: COLLECT_DIAGNOSTICS
      params:
        reason: "Fan failure detected on {name}"

  - condition: "state == 'Absent'"
    verdict: CRITICAL
    message: "Fan {name} has been removed"
    debounce:
      count: 1
      window: 1

  - condition: "speed_rpm < threshold_low_critical AND speed_rpm > 0"
    verdict: CRITICAL
    message: "Fan {name} RPM {speed_rpm} below critical threshold {threshold_low_critical}"

  - condition: "redundancy_health != 'OK'"
    verdict: WARNING
    message: "Fan redundancy degraded: {redundancy_health}"

  - condition: "health == 'Warning'"
    verdict: WARNING
    message: "Fan {name} degraded"

  - condition: "deviation < -2.0"
    verdict: WARNING
    message: "Fan {name} RPM {speed_rpm} is {deviation}σ below baseline mean {baseline_mean}"

trending:
  - field: speed_rpm
    direction: declining
    verdict: TRENDING
    message: "Fan {name} declining at {rate} RPM/hr, projected to reach {threshold} in {time_to_threshold}"
    threshold_field: threshold_low_critical

default_verdict: HEALTHY
```

### 7.2 Disk Health Skill

```yaml
name: disk-health
version: 1
target: disk
description: Detect disk failures, SMART alerts, SSD wear, and predictive wear trends

rules:
  - condition: "health == 'Critical'"
    verdict: CRITICAL
    message: "Disk {name} (slot {slot}) has failed"
    action:
      type: IDENTIFY_LED
      params:
        target: "{name}"
        reason: "Disk failure — identify for replacement"

  - condition: "smart_alert == true"
    verdict: WARNING
    message: "Disk {name} (slot {slot}) SMART predictive failure alert"
    action:
      type: IDENTIFY_LED
      params:
        target: "{name}"
        reason: "SMART predictive failure"

  - condition: "life_left_pct <= 10 AND life_left_pct >= 0"
    verdict: CRITICAL
    message: "Disk {name} SSD life critically low: {life_left_pct}%"
    action:
      type: IDENTIFY_LED
      params:
        target: "{name}"
        reason: "SSD life {life_left_pct}% — replacement urgent"

  - condition: "life_left_pct <= 25 AND life_left_pct > 10"
    verdict: WARNING
    message: "Disk {name} SSD life low: {life_left_pct}%"

  - condition: "raid_status == 'Degraded'"
    verdict: WARNING
    message: "Disk {name} RAID status degraded"

  - condition: "raid_status == 'Rebuilding'"
    verdict: WARNING
    message: "Disk {name} RAID rebuilding"

  - condition: "health == 'Warning'"
    verdict: WARNING
    message: "Disk {name} (slot {slot}) degraded"

trending:
  - field: life_left_pct
    direction: declining
    verdict: TRENDING
    message: "Disk {name} SSD wear: {rate}%/month, replacement in {time_to_threshold}"
    threshold_field: 0

default_verdict: HEALTHY
```

### 7.3 Memory Health Skill

```yaml
name: memory-health
version: 1
target: memory
description: Detect DIMM failures, ECC errors, and correctable error rate trending

rules:
  - condition: "health == 'Critical'"
    verdict: CRITICAL
    message: "DIMM {name} has failed (socket {socket}, channel {channel}, slot {slot})"

  - condition: "alarm_ecc_uncorrectable == true"
    verdict: CRITICAL
    message: "DIMM {name} uncorrectable ECC error detected"
    action:
      type: COLLECT_DIAGNOSTICS
      params:
        reason: "Uncorrectable ECC on {name}"

  - condition: "alarm_ecc_correctable == true"
    verdict: WARNING
    message: "DIMM {name} correctable ECC threshold exceeded (lifetime: {ecc_correctable_lifetime})"

  - condition: "alarm_temperature == true"
    verdict: WARNING
    message: "DIMM {name} thermal alarm"

  - condition: "health == 'Warning'"
    verdict: WARNING
    message: "DIMM {name} degraded"

  - condition: "ecc_correctable_current > 10"
    verdict: WARNING
    message: "DIMM {name} has {ecc_correctable_current} correctable ECC errors since last clear"

trending:
  - field: ecc_correctable_lifetime
    direction: rising
    verdict: TRENDING
    message: "DIMM {name} ECC errors rising at {rate}/hr"

default_verdict: HEALTHY
```

### 7.4 PSU Health Skill

```yaml
name: psu-health
version: 1
target: psu
description: Detect PSU failures, redundancy loss, and power anomalies

rules:
  - condition: "health == 'Critical'"
    verdict: CRITICAL
    message: "PSU {name} has failed"
    action:
      type: COLLECT_DIAGNOSTICS
      params:
        reason: "PSU failure on {name}"

  - condition: "state == 'Absent'"
    verdict: CRITICAL
    message: "PSU {name} has been removed"

  - condition: "redundancy_health != 'OK'"
    verdict: WARNING
    message: "PSU redundancy lost: {redundancy_health} (mode: {redundancy_mode})"

  - condition: "health == 'Warning'"
    verdict: WARNING
    message: "PSU {name} degraded"

  - condition: "input_voltage < 100 AND input_voltage > 0"
    verdict: WARNING
    message: "PSU {name} input voltage low: {input_voltage}V"

trending:
  - field: output_watts
    direction: declining
    verdict: TRENDING
    message: "PSU {name} output declining at {rate}W/hr"

default_verdict: HEALTHY
```

### 7.5 Thermal Health Skill

```yaml
name: thermal-health
version: 1
target: thermal
description: Detect thermal threshold violations, cooling failures, and temperature trends

rules:
  - condition: "reading_c >= threshold_critical AND threshold_critical > 0"
    verdict: CRITICAL
    message: "Sensor {name} at {reading_c}°C — above critical threshold {threshold_critical}°C"
    action:
      type: FAN_RESET
      params:
        reason: "Critical temperature on {name}: {reading_c}°C"

  - condition: "health == 'Critical'"
    verdict: CRITICAL
    message: "Sensor {name} thermal critical"

  - condition: "reading_c >= threshold_warning AND threshold_warning > 0"
    verdict: WARNING
    message: "Sensor {name} at {reading_c}°C — above warning threshold {threshold_warning}°C"

  - condition: "reading_c <= threshold_cold_critical AND threshold_cold_critical != 0"
    verdict: WARNING
    message: "Sensor {name} at {reading_c}°C — below cold critical {threshold_cold_critical}°C (cooling failure?)"

  - condition: "health == 'Warning'"
    verdict: WARNING
    message: "Sensor {name} thermal warning"

  - condition: "deviation > 3.0"
    verdict: WARNING
    message: "Sensor {name} at {reading_c}°C — {deviation}σ above baseline mean {baseline_mean}°C"

trending:
  - field: reading_c
    direction: rising
    verdict: TRENDING
    message: "Sensor {name} rising at {rate}°C/hr, projected to reach {threshold}°C in {time_to_threshold}"
    threshold_field: threshold_critical

default_verdict: HEALTHY
```

---

## 8. Skill Loading and Validation

### 8.1 Loading

- Skills are loaded from `skills.directory` (default `/etc/harkeniq/skills/`) at agent startup
- All `.yaml` files in the directory are loaded
- Skills are reloaded on SIGHUP (systemd `ExecReload`)
- Duplicate skill names are an error (agent logs error and refuses to start)

### 8.2 Validation Rules

| Check | Error |
|-------|-------|
| `name` missing or empty | `SkillValidationError: name is required` |
| `version` not 1 | `SkillValidationError: unsupported schema version {version}` |
| `target` not in (fan, disk, memory, psu, thermal) | `SkillValidationError: unknown target '{target}'` |
| `rules` empty | `SkillValidationError: at least one rule is required` |
| Condition syntax error | `SkillParseError: invalid expression '{condition}': {detail}` |
| Unknown field in condition | `SkillValidationError: unknown field '{field}' for target '{target}'` |
| Unknown verdict | `SkillValidationError: unknown verdict '{verdict}' (expected CRITICAL, WARNING, HEALTHY, TRENDING, UNKNOWN)` |
| Unknown action type | `SkillValidationError: unknown action type '{type}' (allowed: IDENTIFY_LED, COLLECT_DIAGNOSTICS, FAN_RESET)` |
| Trending direction not declining/rising | `SkillValidationError: trending direction must be 'declining' or 'rising'` |
| Debounce count > window | `SkillValidationError: debounce count {count} exceeds window {window}` |

### 8.3 CLI Validation

```bash
harken skills validate
# Validates all skill files in the configured directory
# Exit 0: all valid
# Exit 4: validation errors (prints each error)

harken skills test fan-health
# Dry-run: loads the skill, evaluates against current telemetry, prints verdicts
# Does not change agent state or produce real verdicts
```

---

## 9. Expression Parser Implementation Notes

### 9.1 Tokenizer

Tokens: IDENTIFIER, NUMBER, STRING (single-quoted), OPERATOR (`==`, `!=`, `<`, `>`, `<=`, `>=`), KEYWORD (`AND`, `OR`, `NOT`), EOF.

Whitespace is ignored between tokens. Keywords are case-insensitive (`and`, `AND`, `And` are equivalent).

### 9.2 Parser

Recursive descent parser producing an AST:

```python
# AST node types
@dataclass
class Comparison:
    field: str          # e.g., "speed_rpm"
    operator: str       # e.g., "<"
    value: Any          # e.g., 2000 or "Critical"

@dataclass
class BooleanOp:
    op: str             # "AND" or "OR"
    left: ASTNode
    right: ASTNode

@dataclass
class NotOp:
    operand: ASTNode
```

### 9.3 Evaluator

```python
def evaluate(node: ASTNode, context: dict) -> bool:
    """Evaluate an AST node against a sensor context dict."""
    if isinstance(node, Comparison):
        left = context.get(node.field)
        right = node.value
        # If right side is a field reference (no quotes, not a number)
        if isinstance(right, str) and right in context:
            right = context[right]
        if left is None:
            return False  # Missing data never triggers
        return compare(left, node.operator, right)
    elif isinstance(node, BooleanOp):
        if node.op == "AND":
            return evaluate(node.left, context) and evaluate(node.right, context)
        else:  # OR
            return evaluate(node.left, context) or evaluate(node.right, context)
    elif isinstance(node, NotOp):
        return not evaluate(node.operand, context)
```

### 9.4 Security

- No `eval()` or `exec()` -- expressions are parsed into a safe AST
- No file I/O, network access, or system calls from within expressions
- Field references can only access the normalized sensor context dict
- Unknown fields return None (condition evaluates to false)
- Maximum expression length: 1000 characters (prevent DoS via pathological parsing)
- Maximum AST depth: 20 (prevent stack overflow)
