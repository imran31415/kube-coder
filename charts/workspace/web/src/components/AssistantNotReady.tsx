import { navigate, routeHref } from '../store/router';
import { PROVIDER_KEYS_ANCHOR, PROVIDER_KEYS_PATH, isNotReady } from '../util/assistants';
import './AssistantNotReady.css';

/**
 * "This agent needs an API key" note for a listed-but-not-ready assistant
 * (#702). Renders nothing for a ready one, so callers can drop it in
 * unconditionally next to any picker.
 */
export function AssistantNotReady({
  assistant,
  class: className = '',
}: {
  assistant: { id: string; label?: string; ready?: boolean; notReadyReason?: string } | undefined;
  class?: string;
}) {
  if (!assistant || !isNotReady(assistant)) return null;
  const href = `${routeHref(PROVIDER_KEYS_PATH)}#${PROVIDER_KEYS_ANCHOR}`;

  function openSettings(e: MouseEvent) {
    // Plain clicks route in-app; modified clicks keep new-tab behaviour.
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    navigate(PROVIDER_KEYS_PATH);
    // Same trick as the settings hash redirect: keep the hash so the route
    // scrolls to the provider keys card once it mounts.
    window.history.replaceState({}, '', href);
  }

  return (
    <p class={`assistant-not-ready ${className}`} role="alert">
      {assistant.notReadyReason ||
        `${assistant.label || assistant.id} isn't set up yet.`}{' '}
      <a href={href} onClick={openSettings}>
        Open Provider API keys
      </a>
    </p>
  );
}
