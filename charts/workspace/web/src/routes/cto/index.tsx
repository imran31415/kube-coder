import { useEffect } from 'preact/hooks';
import { newChatMode } from '../../store/hypervisor';
import { navigate } from '../../store/router';

/**
 * `/cto` is no longer a page (#683) — it is a permanent redirect into Chat with
 * CTO mode pre-selected.
 *
 * The AI CTO was never a separate product: it was a chat thread with a
 * different system preamble and three extra side panels, all of which now live
 * in Chat itself. The path stays resolvable so bookmarks, the Feed's "Discuss
 * with CTO" handoff and every doc link that ever pointed here keep working.
 * It is not a nav destination on web or mobile.
 */
export function CtoRoute() {
  useEffect(() => {
    // Pre-select the mode so the landing is the AI CTO, not a plain chat. A
    // replace (not a push) keeps Back going where the user came from rather
    // than bouncing them through the redirect again.
    newChatMode.value = 'cto';
    navigate('/hypervisor', true);
  }, []);
  return null;
}
