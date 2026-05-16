import { toasts, dismissToast } from '../store/ui';
import './Toast.css';

export function ToastRack() {
  const items = toasts.value;
  if (items.length === 0) return null;
  return (
    <div class="toast-rack" role="status" aria-live="polite">
      {items.map((t) => (
        <button
          key={t.id}
          class={`toast toast-${t.kind}`}
          onClick={() => dismissToast(t.id)}
          aria-label="Dismiss toast"
        >
          {t.message}
        </button>
      ))}
    </div>
  );
}
