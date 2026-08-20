import { type CSSProperties } from "react";
import { AreaChart, Area, ResponsiveContainer } from "recharts";

interface Props {
  title: string;
  value: string | number;
  unit?: string;
  trend?: "up" | "down" | "flat";
  trendValue?: string;
  sparklineData?: number[];
}

const cardStyle: CSSProperties = {
  background: "var(--bg-card)",
  border: "1px solid var(--border-color)",
  borderRadius: "var(--radius-lg)",
  padding: "1rem 1.25rem",
  boxShadow: "var(--shadow-sm)",
  display: "flex",
  flexDirection: "column",
  gap: "0.375rem",
};

const titleStyle: CSSProperties = {
  fontSize: "0.75rem",
  fontWeight: 500,
  color: "var(--text-muted)",
  textTransform: "uppercase",
  letterSpacing: "0.04em",
};

const valueRowStyle: CSSProperties = {
  display: "flex",
  alignItems: "baseline",
  gap: "0.5rem",
};

const valueStyle: CSSProperties = {
  fontSize: "1.75rem",
  fontWeight: 700,
  lineHeight: 1.2,
  color: "var(--text-primary)",
};

const unitStyle: CSSProperties = {
  fontSize: "0.875rem",
  color: "var(--text-secondary)",
};

const trendColors = {
  up: "var(--status-ok)",
  down: "var(--status-critical)",
  flat: "var(--text-muted)",
};

const trendArrows = {
  up: "\u2191",
  down: "\u2193",
  flat: "\u2192",
};

export default function MetricCard({ title, value, unit, trend, trendValue, sparklineData }: Props) {
  return (
    <div style={cardStyle}>
      <div style={titleStyle}>{title}</div>
      <div style={valueRowStyle}>
        <span style={valueStyle}>{value}</span>
        {unit && <span style={unitStyle}>{unit}</span>}
        {trend && (
          <span
            style={{
              fontSize: "0.8125rem",
              fontWeight: 600,
              color: trendColors[trend],
              display: "flex",
              alignItems: "center",
              gap: "0.125rem",
            }}
          >
            {trendArrows[trend]} {trendValue}
          </span>
        )}
      </div>
      {sparklineData && sparklineData.length > 1 && (
        <div style={{ height: 40, marginTop: "0.25rem" }}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={sparklineData.map((v, i) => ({ i, v }))}>
              <defs>
                <linearGradient id="sparkGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.15} />
                  <stop offset="100%" stopColor="var(--accent)" stopOpacity={0} />
                </linearGradient>
              </defs>
              <Area
                type="monotone"
                dataKey="v"
                stroke="var(--accent)"
                strokeWidth={1.5}
                fill="url(#sparkGrad)"
                dot={false}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
