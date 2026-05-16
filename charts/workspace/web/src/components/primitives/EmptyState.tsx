import type { ComponentChildren } from 'preact';
import './EmptyState.css';

export interface EmptyStateProps {
  icon?: ComponentChildren;
  title: string;
  description?: ComponentChildren;
  action?: ComponentChildren;
}

export function EmptyState({ icon, title, description, action }: EmptyStateProps) {
  return (
    <div class="empty-state">
      {icon && <div class="empty-state-icon" aria-hidden>{icon}</div>}
      <h2 class="empty-state-title">{title}</h2>
      {description && <p class="empty-state-desc muted">{description}</p>}
      {action && <div class="empty-state-action">{action}</div>}
    </div>
  );
}
