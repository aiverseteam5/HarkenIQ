import { AreaChart, Area, ResponsiveContainer } from "recharts";
import { action } from "../design/tokens";

interface Props {
  title: string;
  value: string | number;
  unit?: string;
  trend?: "up" | "down" | "flat";
  trendValue?: string;
  sparklineData?: number[];
}

const TREND_ARROW = {
  up: "↑",
  down: "↓",
  flat: "→",
};

const TREND_WORD = {
  up: "up",
  down: "down",
  flat: "flat",
};

/** A35.6: a KPI. Its trend shows a direction and never a verdict: an
 *  arrow up is not good and an arrow down is not bad until the server says
 *  which is which, so neither is coloured (A35.5). */
export default function MetricCard({ title, value, unit, trend, trendValue, sparklineData }: Props) {
  return (
    <div className="metric-card">
      <div className="metric-card__title">{title}</div>
      <div className="metric-card__value-row">
        <span className="metric-card__value">{value}</span>
        {unit && <span className="metric-card__unit">{unit}</span>}
        {trend && (
          <span className="metric-card__trend">
            <span aria-hidden="true">{TREND_ARROW[trend]}</span>
            <span className="sr-only">{TREND_WORD[trend]}</span> {trendValue}
          </span>
        )}
      </div>
      {sparklineData && sparklineData.length > 1 && (
        <div className="metric-card__spark" aria-hidden="true">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={sparklineData.map((v, i) => ({ i, v }))}>
              <Area
                type="monotone"
                dataKey="v"
                stroke={action.primaryBg}
                strokeWidth={1.5}
                fill={action.primaryBg}
                fillOpacity={0.08}
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
