# Document 13: Baseline and Trending Algorithm Specification

**Purpose:** Implementation-ready specification for per-device sensor baselining and predictive failure trending in HarkenIQ R1.
**Scope:** Baseline learning, anomaly detection via statistical deviation, and linear regression trending for Dell PowerEdge and HPE ProLiant servers.
**Status:** Draft.

---

## 1. Overview

Every sensor on every device has its own definition of "normal." A fan that idles at 4,800 RPM in a cool aisle behaves differently from an identical fan in a hot aisle running at 7,200 RPM. Fleet-wide averages obscure this. HarkenIQ baselines are **per-sensor, per-device** -- the agent learns what normal looks like for each individual reading on each individual machine.

The baseline subsystem serves two purposes:

1. **Anomaly detection.** Once the agent knows a sensor's normal range (mean and standard deviation), it can flag deviations that the static Redfish thresholds would miss -- a fan trending downward within its "acceptable" range, for example.
2. **Predictive failure trending.** By fitting a linear regression to the time-series, the agent can project when a sensor will cross a threshold and emit TRENDING verdicts with time-to-threshold estimates, giving operators lead time to act before hardware fails.

Baselines are never fleet-wide. The agent runs on each device and maintains baselines only for the sensors it can see on the local BMC.

---

## 2. Baseline Algorithm

### 2.1 Data Structure

Each sensor tracked by the agent maintains the following baseline state:

| Field | Type | Description |
|-------|------|-------------|
| `ring_buffer` | `list[(timestamp, value)]` | Fixed-size FIFO of recent samples |
| `buffer_size` | `int` | Configurable ring buffer capacity (default 1440) |
| `sample_count` | `int` | Total samples ingested (may exceed `buffer_size` after eviction) |
| `mean` | `float` | Running mean (Welford's algorithm) |
| `variance` | `float` | Running variance (Welford's algorithm, population) |
| `stddev` | `float` | `sqrt(variance)` |
| `min_val` | `float` | Minimum observed value in the ring buffer |
| `max_val` | `float` | Maximum observed value in the ring buffer |
| `first_sample_at` | `datetime` | Timestamp of the oldest sample in the buffer |
| `last_sample_at` | `datetime` | Timestamp of the most recent sample |
| `degraded_baseline` | `bool` | True if baseline was learned during a WARNING state |

Each sample is a `(timestamp, value)` tuple. At the default 60-second polling interval, the ring buffer holds 1,440 samples (24 hours).

### 2.2 Welford's Online Algorithm

Baseline statistics are maintained incrementally using Welford's online algorithm for numerical stability. This avoids catastrophic cancellation that naive `sum(x^2) - sum(x)^2 / n` formulas produce with large counts or similar values.

```python
def update_welford(state, new_value):
    """Incremental mean and variance (Welford's online algorithm)."""
    state.sample_count += 1
    delta = new_value - state.mean
    state.mean += delta / state.sample_count
    delta2 = new_value - state.mean
    state.m2 += delta * delta2
    state.variance = state.m2 / state.sample_count  # population variance
    state.stddev = math.sqrt(state.variance)
```

When a sample is evicted from the ring buffer (FIFO), the inverse operation is applied:

```python
def remove_welford(state, old_value):
    """Remove oldest sample from running statistics."""
    if state.sample_count <= 1:
        state.mean = 0.0
        state.m2 = 0.0
        state.variance = 0.0
        state.stddev = 0.0
        state.sample_count = 0
        return
    delta = old_value - state.mean
    state.sample_count -= 1
    state.mean -= delta / state.sample_count
    delta2 = old_value - state.mean
    state.m2 -= delta * delta2
    state.variance = state.m2 / state.sample_count
    state.stddev = math.sqrt(max(0.0, state.variance))  # guard against float drift
```

Min and max are recomputed from the ring buffer only when the evicted sample equals the current min or max. This is O(n) in the worst case but occurs rarely.

### 2.3 Confidence Metric

Confidence quantifies how much data the baseline has accumulated:

```
confidence = min(1.0, sample_count / min_samples)
```

Where `min_samples` defaults to 60 (configurable via `baseline.min_samples`).

| Confidence Range | Label | Behavior |
|------------------|-------|----------|
| 0.00 -- 0.49 | **Learning** | Only BMC `Status.Health` values (OK / Warning / Critical) are passed through as verdicts. No expression-based skill evaluation. No trending. TUI displays "LEARNING" indicator next to the sensor. |
| 0.50 -- 0.99 | **Low confidence** | Threshold-based skill verdicts are enabled (expressions can reference sensor values). Trending regression is warming up but not yet emitting verdicts. |
| 1.00 | **Established** | Full verdicts and trending enabled. All skill expressions and trending projections are active. |

The boundary between "Learning" and "Low confidence" is at `sample_count = 30` (half of the default `min_samples = 60`). At 60-second polling, this corresponds to ~30 minutes before threshold verdicts activate and ~60 minutes before full confidence.

### 2.4 Learning Mode Behavior

During the Learning phase (confidence < 0.50):

1. **TUI display.** The sensor row shows a "LEARNING" indicator and the current sample count (e.g., `LEARNING 23/60`).
2. **Verdicts.** Only the BMC's own `Status.Health` field is passed through. If the BMC reports `Warning` or `Critical`, the agent emits that verdict directly -- no expression evaluation needed.
3. **No skill evaluation.** Expression-based skills (threshold comparisons, deviation checks) are skipped for this sensor.
4. **No trending.** Linear regression is not computed.
5. **Checkpoint saves partial data.** The ring buffer and Welford state are persisted to SQLite on every checkpoint cycle (600s). If the agent restarts, learning resumes from the checkpoint rather than starting over.

### 2.5 Baseline Update Rules

On each sensor poll:

1. Add the new `(timestamp, value)` sample to the ring buffer.
2. If the ring buffer is full, evict the oldest sample (FIFO) and apply `remove_welford`.
3. Apply `update_welford` with the new value.
4. Update `min_val` and `max_val`.
5. Update `last_sample_at`.

**Exception -- CRITICAL state freeze:**
- The baseline is **never updated** while the sensor is in a CRITICAL state. This prevents the agent from learning a fault condition as "normal."
- After the sensor recovers from CRITICAL (verdict transitions to WARNING or HEALTHY), the agent waits **5 samples** (configurable via `baseline.critical_pause_samples`) before resuming baseline updates. This debounces the recovery and avoids learning transitional readings.

---

## 3. Trending Algorithm

### 3.1 Linear Regression

Trending uses ordinary least squares (OLS) linear regression on the sensor's ring buffer to detect gradual drift toward a threshold.

- **X axis:** Time in hours, relative to the first sample in the ring buffer (i.e., `x_i = (timestamp_i - first_sample_at) / 3600`).
- **Y axis:** Sensor value (RPM, degrees C, percentage, error count/rate).
- **Output:** Slope (units/hour), intercept, and R-squared (coefficient of determination).

### 3.2 Incremental Implementation

The regression is maintained incrementally using running sums. No full matrix solve is needed.

Maintained state:

| Field | Description |
|-------|-------------|
| `sum_x` | Sum of all x values (hours) |
| `sum_y` | Sum of all y values (sensor readings) |
| `sum_xy` | Sum of x*y products |
| `sum_x2` | Sum of x-squared values |
| `sum_y2` | Sum of y-squared values |
| `n` | Number of samples in the regression |

Formulas:

```
slope = (n * sum_xy - sum_x * sum_y) / (n * sum_x2 - sum_x^2)

intercept = (sum_y - slope * sum_x) / n

r_squared = (n * sum_xy - sum_x * sum_y)^2 /
            ((n * sum_x2 - sum_x^2) * (n * sum_y2 - sum_y^2))
```

When a sample is evicted from the ring buffer, its contribution is subtracted from all five sums. When a new sample is added, its contribution is added. This keeps the regression current in O(1) per sample.

**Numerical note:** After long runs (tens of thousands of samples), floating-point drift in the running sums can accumulate. The implementation should periodically recompute the sums from the ring buffer (e.g., every 1,000 evictions) as a correction step. This is O(n) but occurs infrequently.

### 3.3 Trending Verdict Rules

A TRENDING verdict is emitted when all of the following conditions are met:

1. **Minimum samples.** The ring buffer contains at least `trending.min_samples` readings (default 60, configurable). At 60-second polling, this is ~1 hour of data.
2. **Slope significance.** `|slope| > slope_threshold`. The default `slope_threshold` is 0.05, meaning 5% of the sensor's nominal range per hour. For a fan with a range of 0--18,000 RPM, this means a slope steeper than 900 RPM/hour.
3. **Goodness of fit.** `R-squared > r_squared_min` (default 0.5). Below this, the data is too noisy for the linear model to be meaningful.
4. **Direction filter.** Only report trends moving TOWARD a threshold (degradation, not recovery):

| Sensor Type | Reportable Trend | Reason |
|-------------|-----------------|--------|
| Fan RPM | Declining (toward `threshold_low_critical`) | Fan slowing = bearing wear or obstruction |
| Temperature | Rising (toward `threshold_critical`) | Heating up = cooling failure |
| SSD wear life | Declining (toward 0%) | Media exhaustion |
| ECC error rate | Rising (any upward trend) | Memory degradation |
| PSU output voltage/efficiency | Declining | Capacitor aging or load imbalance |

**Do NOT report:**

| Sensor Type | Ignored Trend | Reason |
|-------------|--------------|--------|
| Temperature | Declining | Cooling down is healthy behavior |
| Fan RPM | Increasing | Fan speed-up is a response to thermal load, not a fault |

### 3.4 Time-to-Threshold Projection

When a TRENDING verdict is warranted, the agent projects when the sensor will cross the relevant threshold:

```
time_to_threshold = (threshold_value - current_value) / slope
```

For declining sensors (negative slope toward a lower threshold), the formula yields a positive result because both `(threshold - current)` and `slope` are negative.

**Guardrails:**
- Only report if `time_to_threshold > 0` (the trend is moving toward the threshold, not away from it).
- Only report if `time_to_threshold < 90 days` (configurable via `trending.max_projection_days`). Beyond 90 days, the projection is too speculative to be actionable. The linear assumption breaks down over such long horizons.
- Format the projection in human-readable form: `"projected to reach [threshold_name] in [X days/hours]"`.

### 3.5 Trending Verdict Output

TRENDING verdicts are structured as `TrendResult` objects:

```python
@dataclass
class TrendResult:
    sensor_id: str              # e.g., "fan:System Board Fan1A"
    field: str                  # e.g., "speed_rpm"
    slope: float                # Units per hour (negative = declining)
    r_squared: float            # Goodness of fit (0.0 to 1.0)
    direction: str              # "rising" or "declining"
    current_value: float        # Most recent sensor reading
    threshold_name: str         # e.g., "threshold_low_critical"
    threshold_value: float      # The threshold being approached
    time_to_threshold_hours: float  # Projected hours until threshold breach
    confidence: float           # Baseline confidence (0.0 to 1.0)
    message: str                # Human-readable summary
```

Example:

```python
TrendResult(
    sensor_id="fan:System Board Fan1A",
    field="speed_rpm",
    slope=-8.5,                     # RPM per hour (negative = declining)
    r_squared=0.87,
    direction="declining",
    current_value=9200,
    threshold_name="threshold_low_critical",
    threshold_value=480,
    time_to_threshold_hours=1027.1, # ~42.8 days
    confidence=1.0,
    message="Fan System Board Fan1A declining at -8.5 RPM/hr, "
            "projected to reach critical in 42 days"
)
```

TRENDING verdicts appear in the TUI event feed and are reported to the Site Manager (stub in R1) alongside threshold verdicts. They do not replace threshold verdicts -- a sensor can simultaneously have a HEALTHY threshold verdict and a TRENDING projection.

---

## 4. Integration with Skill Evaluation

### 4.1 Evaluation Order

On each sensor poll, the processing pipeline is:

1. **Baseline update.** New sample added to ring buffer, Welford statistics updated.
2. **Skill evaluation.** Expression-based skills are evaluated against the current sensor values.
3. **Trending evaluation.** Linear regression is computed and TRENDING verdicts are emitted if warranted.
4. **Debounce.** N-of-M debounce is applied to threshold-based verdicts (not trending verdicts).
5. **Verdict emission.** Final verdicts (threshold + trending) are emitted to the TUI and reported to the Site Manager.

### 4.2 Baseline Fields Available to Skills

Skills can reference the following baseline-derived fields in their condition expressions:

| Field | Type | Description |
|-------|------|-------------|
| `baseline_mean` | `float` | Current running mean for this sensor |
| `baseline_stddev` | `float` | Current running standard deviation |
| `deviation` | `float` | Z-score: `(current_value - baseline_mean) / baseline_stddev` |

Example skill condition using baseline deviation:

```yaml
conditions:
  - field: fan.speed_rpm
    expression: "deviation < -2.0"
    severity: WARNING
    message: "Fan speed is {deviation:.1f} standard deviations below baseline mean ({baseline_mean:.0f} RPM)"
```

### 4.3 Trending and Debounce

- **Threshold verdicts** (HEALTHY, WARNING, CRITICAL) are subject to N-of-M debounce as defined in Document 6:
  - CRITICAL: 2 of last 3 polls
  - WARNING: 3 of last 5 polls
  - Recovery to HEALTHY: 3 consecutive healthy
- **TRENDING verdicts are NOT debounced.** Trending is inherently gradual and statistical. Debouncing a regression result (which already aggregates dozens or hundreds of samples) would add latency to detection without reducing false positives. Trending verdicts are emitted or cleared immediately based on the current regression output.

---

## 5. Edge Cases

### 5.1 All Samples Identical

When every sample in the ring buffer has the same value (e.g., a binary status sensor or a fan at fixed speed):

- `stddev = 0`, coefficient of variation = 0.
- **Verdict:** HEALTHY (the sensor is perfectly stable).
- **Trending:** `slope = 0`, no TRENDING verdict emitted.
- **Deviation calculation:** When `stddev = 0`, the z-score `deviation` is defined as `0` (no deviation from a constant baseline). The implementation must guard against division by zero in `(current - mean) / stddev`.

### 5.2 Sudden Discontinuity (Sensor Replacement or Reset)

When a sensor reading jumps dramatically (e.g., a fan module is replaced, or a BMC firmware update resets a counter):

- **Detection:** The new value differs from the baseline mean by more than `5 * stddev`.
- **Action:** Reset the baseline for this sensor entirely. Clear the ring buffer, zero the Welford state, and re-enter learning mode.
- **Log:** `"Baseline reset for {sensor_id}: value {new_value} deviates {z:.1f} sigma from mean {mean:.1f}"`
- **Rationale:** This prevents a false CRITICAL verdict when the new sensor's "normal" is legitimately different from the old one. It also avoids contaminating the old baseline with the new sensor's values.

### 5.3 Baseline Learned During Degraded State

If the agent starts monitoring a device that is already in a WARNING state, the baseline will encode the degraded readings as "normal."

- **Detection:** Confidence reaches 1.0, but `Status.Health` was not `OK` during the majority (>50%) of the learning window.
- **Action:** Flag the baseline as `degraded_baseline = True` in the checkpoint data.
- **Impact:**
  - Trending still works correctly (it detects *further* degradation from the degraded baseline).
  - Absolute threshold verdicts from static Redfish thresholds are unaffected (they do not depend on the learned baseline).
  - Deviation-based skill conditions may miss the original fault because the degraded state IS the baseline.
- **TUI display:** Show a warning indicator next to the sensor: `"baseline learned during warning state"`.

### 5.4 Clock Skew / Time Jump

If the system clock jumps (NTP correction, manual adjustment, VM migration):

- **Detection:** Time between consecutive samples exceeds `5 * expected_interval` (300 seconds at 60-second polling).
- **Action:**
  - Insert a gap marker in the ring buffer at the discontinuity.
  - Exclude the gap from the linear regression (treat the data as two separate segments; only the most recent contiguous segment contributes to the regression).
  - Continue Welford baseline updates normally (mean/stddev are not time-dependent).
- **Log:** `"Time gap detected: {gap_seconds}s between samples for {sensor_id}"`

### 5.5 Sensor Oscillation

A sensor value oscillating around a threshold (e.g., 46 C to 48 C around a 47 C warning threshold):

- **Threshold verdicts:** The N-of-M debounce mechanism handles this. The verdict flips only after sustained readings on one side (3 of 5 for WARNING).
- **Trending:** Oscillation produces a near-zero slope and low R-squared. Neither condition for a TRENDING verdict is met, so no false trending alarm is emitted.
- **Baseline:** The oscillation is captured in the baseline's stddev, making the baseline a truthful representation of the sensor's behavior.

### 5.6 Counter-Type Sensors (ECC Error Counts)

ECC error counts are monotonically increasing counters, not instantaneous gauges. Baselining and trending the raw count is meaningless -- a healthy DIMM with a long uptime will have a higher count than a recently rebooted one.

- **Baseline:** Track the **rate of change** (delta per hour), not the absolute value. On each poll, compute `delta = current_count - previous_count` and baseline the delta.
- **Trending:** Linear regression is performed on the **rate of change** over time, not the raw count. An increasing rate of ECC errors (accelerating degradation) triggers a TRENDING verdict.
- **Counter reset detection:** If `current_count < previous_count`, the counter has been reset (BMC reboot, firmware update). Log `"ECC counter reset detected for {sensor_id}"` and restart rate tracking from the new baseline.
- **Zero-rate handling:** A DIMM producing zero errors per hour has `delta = 0` for all samples. This falls under the "all samples identical" case (Section 5.1): stddev = 0, slope = 0, verdict = HEALTHY.

---

## 6. Configuration

All baseline and trending parameters are configurable in `/etc/harkeniq/config.yaml`:

```yaml
# Baseline and trending configuration
baseline:
  window_samples: 1440        # Ring buffer size (24h at 60s polling)
  min_samples: 60             # Minimum samples before baseline is trusted (confidence = 1.0)
  max_age_days: 30            # Discard baselines older than this on load
  critical_pause_samples: 5   # Wait N samples after CRITICAL recovery before updating baseline

trending:
  min_samples: 60             # Minimum samples before trending verdicts are emitted
  slope_threshold: 0.05       # Slope significance threshold (fraction of sensor range per hour)
  r_squared_min: 0.5          # Minimum R-squared for trend to be significant
  max_projection_days: 90     # Don't project beyond this horizon (too speculative)

debounce:
  critical: [2, 3]            # 2 of last 3 polls must be CRITICAL to emit CRITICAL verdict
  warning: [3, 5]             # 3 of last 5 polls must be WARNING to emit WARNING verdict
  recovery: [3, 3]            # 3 consecutive HEALTHY polls to transition back to HEALTHY
```

**Parameter rationale:**

| Parameter | Default | Rationale |
|-----------|---------|-----------|
| `window_samples: 1440` | 24 hours at 60s polling | Captures full diurnal cycle (day/night workload variation) |
| `min_samples: 60` | ~1 hour at 60s polling | Enough data to compute a meaningful mean and stddev |
| `max_age_days: 30` | 30 days | Baselines older than this are stale (firmware updates, hardware changes) |
| `critical_pause_samples: 5` | 5 minutes at 60s polling | Debounces recovery; avoids learning transitional sensor readings |
| `slope_threshold: 0.05` | 5% of range per hour | Below this, drift is within normal variation |
| `r_squared_min: 0.5` | -- | Below 0.5, the linear model explains less than half the variance (noise) |
| `max_projection_days: 90` | 3 months | Linear extrapolation beyond 90 days is unreliable |

---

## 7. Checkpoint Persistence

Baseline and trending state must survive agent restarts. State is persisted to the SQLite checkpoint database (WAL mode, as defined in Document 6).

### 7.1 Schema

```sql
CREATE TABLE IF NOT EXISTS baselines (
    sensor_id       TEXT PRIMARY KEY,
    mean            REAL NOT NULL,
    variance        REAL NOT NULL,
    stddev          REAL NOT NULL,
    m2              REAL NOT NULL,          -- Welford's M2 accumulator
    min_val         REAL NOT NULL,
    max_val         REAL NOT NULL,
    sample_count    INTEGER NOT NULL,
    first_sample_at TEXT NOT NULL,          -- ISO 8601 timestamp
    last_sample_at  TEXT NOT NULL,          -- ISO 8601 timestamp
    degraded_baseline INTEGER NOT NULL DEFAULT 0,  -- boolean flag
    samples_json    TEXT NOT NULL,          -- JSON array of [timestamp, value] pairs (ring buffer)

    -- Trending regression state (incremental sums)
    reg_sum_x       REAL NOT NULL DEFAULT 0.0,
    reg_sum_y       REAL NOT NULL DEFAULT 0.0,
    reg_sum_xy      REAL NOT NULL DEFAULT 0.0,
    reg_sum_x2      REAL NOT NULL DEFAULT 0.0,
    reg_sum_y2      REAL NOT NULL DEFAULT 0.0,
    reg_n           INTEGER NOT NULL DEFAULT 0,

    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
```

### 7.2 Checkpoint Lifecycle

- **Write frequency:** Every checkpoint cycle (600 seconds), as part of the global checkpoint sweep.
- **Write scope:** Only sensors whose baseline has changed since the last checkpoint are written (dirty flag per sensor).
- **On startup:** Load all rows from `baselines`. Reconstruct the ring buffer from `samples_json`. Recalculate `confidence` from `sample_count`. Discard rows where `last_sample_at` is older than `baseline.max_age_days`.
- **On clean shutdown:** Force a final checkpoint write for all dirty baselines.
- **Crash recovery:** The agent resumes from the last checkpoint. At most 600 seconds of baseline data is lost (10 samples at 60-second polling). Confidence may drop slightly but recovers within minutes.

### 7.3 Storage Budget

| Component | Size per Sensor | Notes |
|-----------|----------------|-------|
| Fixed fields | ~120 bytes | mean, variance, stddev, m2, min, max, counts, timestamps, regression sums |
| Ring buffer (JSON) | ~23 KB | 1440 samples x 16 bytes average per `[timestamp, value]` pair |
| **Total per sensor** | **~23 KB** | |
| **50 sensors** | **~1.2 MB** | Well within the 256 MB memory ceiling |

---

## 8. Performance

### 8.1 Computational Cost

| Operation | Complexity | Notes |
|-----------|-----------|-------|
| Baseline update (Welford) | O(1) per sample | Addition, subtraction, division |
| Regression update | O(1) per sample | Five running sums maintained incrementally |
| Min/max update | O(1) amortized | O(n) recomputation only when evicted sample was the extremum |
| Periodic sum correction | O(n) every 1,000 evictions | Guards against floating-point drift |
| Checkpoint write | O(n) per sensor | Serialize ring buffer to JSON; only dirty sensors |

### 8.2 Memory Footprint

At 50 sensors per device:

- **In-memory baseline state:** ~1.2 MB total (ring buffers dominate)
- **SQLite on disk:** ~1.2 MB per device (mirrors in-memory state)
- **Overhead:** Welford accumulators and regression sums add negligible fixed cost (~120 bytes per sensor)

This is well within the R1 resource ceiling of 256 MB RAM and <1 MB writes per checkpoint cycle.

### 8.3 No Batch Recomputation

Because both the baseline statistics (Welford) and regression state (running sums) are maintained incrementally and checkpointed, there is no need for batch recomputation on restart. The agent loads the checkpoint and resumes O(1) updates immediately. This is a deliberate design choice to minimize startup latency and CPU usage.

---

## 9. Worked Examples

### 9.1 Fan Degradation (Happy Path)

1. Agent starts monitoring `fan:System Board Fan1A` on a Dell R750.
2. First 60 polls (~1 hour): Learning mode. TUI shows `LEARNING 1/60`, `LEARNING 2/60`, ... `LEARNING 60/60`. Only BMC `Status.Health = OK` is reported.
3. Poll 61: Confidence = 1.0. Baseline established: mean = 9,500 RPM, stddev = 150 RPM. Threshold and trending verdicts enabled.
4. Over the next 24 hours (polls 61--1440): Fan speed gradually declines due to bearing wear. Slope = -8.5 RPM/hr, R-squared = 0.87.
5. Poll ~200 (after ~3.3 hours with sufficient data): Trending regression meets all criteria. TRENDING verdict emitted: `"Fan System Board Fan1A declining at -8.5 RPM/hr, projected to reach critical in 42 days"`.
6. Operator sees the TRENDING event in TUI. Has 42 days to schedule fan replacement.

### 9.2 Temperature Oscillation (Debounce Prevents Flapping)

1. Inlet temperature oscillates: 46 C, 47 C, 48 C, 47 C, 46 C, 48 C. Warning threshold = 47 C.
2. Debounce requires 3-of-5 WARNING polls. Oscillation keeps the count at 2-of-5 or 3-of-5, preventing rapid verdict flapping.
3. Trending: slope is near zero, R-squared is low. No TRENDING verdict.
4. Baseline: mean = 47 C, stddev = 0.8 C. Accurately captures the oscillation.

### 9.3 ECC Counter (Rate-Based Tracking)

1. DIMM reports ECC count: 0, 0, 0, 0, 1, 1, 1, 1, 1, 2, 2, 2, 3, 3, 4, 5, 7, 10 ...
2. Agent computes deltas per poll: 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 1, 0, 1, 1, 2, 3 ...
3. Baseline tracks the delta rate. Early baseline: mean ~0.1 errors/poll.
4. As the DIMM degrades, the rate accelerates. Trending detects a positive slope in the rate.
5. TRENDING verdict: `"ECC error rate for DIMM A1 rising at 0.3 errors/hr, projected to reach replacement threshold in 14 days"`.
