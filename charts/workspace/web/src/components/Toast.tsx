import { toasts, dismissToast } from '../store/ui';
import './Toast.css';

export function ToastRack() {
  const items = toasts.value;
  // The rack always renders (even empty) so the live region exists before any
  // toast lands — regions inserted together with their content are often not
  // announced by screen readers.
  return (
    <div class="toast-rack" role="status" aria-live="polite">
      {items.map((t) => (
        <button
          key={t.id}
          class={`toast toast-${t.kind}`}
          onClick={() => dismissToast(t.id)}
          title="Dismiss"
        >
          {t.message}
        </button>
      ))}
    </div>
  );
}
