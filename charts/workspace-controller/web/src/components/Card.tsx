import type { Series } from '../api/workspaces';
import { Sparkline } from './Sparkline';

/** Compact stat card with an optional sparkline. Shared by the inline mini
 *  panel and (without spark) the detail page foot. */
export function Card({
  title,
  value,
  sub,
  t,
  spark,
  sparkColor,
}: {
  title: string;
  value: string;
  sub: string;
  t: 'ok' | 'warn' | 'crit';
  spark?: Series;
  sparkColor?: string;
}) {
  return (
    <div class={`card tone-${t}`}>
      <div class="card-title">{title}</div>
      <div class="card-value">{value}</div>
      {sub && <div class="card-sub">{sub}</div>}
      {spark && spark.length > 1 && <Sparkline points={spark} color={sparkColor} />}
    </div>
  );
}
