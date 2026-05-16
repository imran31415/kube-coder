import { useEffect, useState } from 'preact/hooks';
import { getHealth } from '../api/system';
import './HealthDot.css';

type State = 'unknown' | 'healthy' | 'degraded' | 'down';

export function HealthDot() {
  const [state, setState] = useState<State>('unknown');
  const [tip, setTip] = useState<string>('Checking…');

  async function tick() {
    try {
      const h = await getHealth();
      const checks = [h.vscode, h.terminal, h.browser];
      const ok = checks.filter(Boolean).length;
      if (ok === checks.length) {
        setState('healthy');
        setTip('All workspace services healthy');
      } else if (ok > 0) {
        setState('degraded');
        setTip(`${ok}/${checks.length} services up`);
      } else {
        setState('down');
        setTip('All services down');
      }
    } catch {
      setState('down');
      setTip('Health endpoint unreachable');
    }
  }

  useEffect(() => {
    void tick();
    const id = setInterval(tick, 30_000);
    return () => clearInterval(id);
  }, []);

  return (
    <span class={`health-dot health-dot-${state}`} title={tip} aria-label={tip} role="status">
      <span class="health-dot-core" />
    </span>
  );
}
