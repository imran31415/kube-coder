import { GuidePanel } from '../../components/GuidePanel';
import { WalkieTalkie } from '../hypervisor/WalkieTalkie';
import './walkie-route.css';

/**
 * Walkie-Talkie route — the in-app internal loopback preview (issue #306),
 * promoted from a mode tucked inside the Hypervisor tab to its own top-level
 * page. The device UI itself lives in `../hypervisor/WalkieTalkie`; this route
 * gives it a full-height container (and a "how it works" guide) so it centers
 * and fills the main pane the way the other routes do. The Hypervisor topbar
 * still links here.
 */
export function WalkieRoute() {
  return (
    <div class="route-walkie">
      <GuidePanel
        title="How the Walkie-Talkie works"
        storageKey="kc.guide.walkie"
        intro="An internal loopback channel to your workspace. Type a message and it runs through the real Conversation Gateway pipeline — driving a real Hypervisor turn — and comes back as chat bubbles with tap-buttons. Right now only the internal loopback transport is connected; other providers will be added soon. The agent and the whole pipeline are real; only the transport is simulated."
        steps={[
          {
            title: 'It links automatically',
            body: 'On open the preview pairs itself — you’ll see the code exchange end in “✅ Linked” in the transcript.',
          },
          {
            title: 'Push to talk',
            body: 'Type a message and press PTT (or Enter). It becomes a real inbound message on the gateway.',
          },
          {
            title: 'Read the reply',
            body: 'Responses come back as chat bubbles with tap-buttons, chunked to ≤4096 characters just like a real channel would deliver.',
          },
          {
            title: 'Test the template path',
            body: 'Flip “Simulate out-of-window” to send your next message as an approved template bubble instead of a normal reply.',
          },
        ]}
        scenarios={[
          { prompt: 'status', outcome: 'the agent replies with live workspace status as a chat message' },
          { prompt: 'tap a quick-reply button', outcome: 'sends it back through the gateway like a real user tap' },
          { prompt: 'toggle out-of-window, then send', outcome: 'your message goes out as a template bubble' },
        ]}
      />
      <div class="route-walkie-body">
        <WalkieTalkie />
      </div>
    </div>
  );
}
