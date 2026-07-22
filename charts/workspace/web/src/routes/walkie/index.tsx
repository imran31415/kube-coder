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
        intro="A voice-first channel to your workspace. Press the orb, speak, and your words run through the real Conversation Gateway pipeline — driving a real Hypervisor turn — then the answer appears on the response card and is read aloud. Right now only the internal loopback transport is connected; other providers will be added soon. The agent and the whole pipeline are real; only the transport is simulated."
        steps={[
          {
            title: 'It links automatically',
            body: 'On open the preview pairs itself over the loopback channel — the status chip flips to LINKED.',
          },
          {
            title: 'Push to talk',
            body: 'Tap the orb and speak — the rings react to your voice and your words appear live. Tap again (or pause) to send. Prefer typing? Use “Type instead” below the orb.',
          },
          {
            title: 'Hear (and read) the reply',
            body: 'The answer lands on the response card with tap-buttons, and — with the speaker on — is spoken aloud. Replay or stop playback next to the card; pressing the orb mid-reply interrupts it.',
          },
          {
            title: 'Test the template path',
            body: 'Open the settings popover (gear, top right) and flip “Simulate out-of-window” to send your next message as an approved template instead of a normal reply. Reset lives there too.',
          },
        ]}
        scenarios={[
          { prompt: '“What’s running right now?” (spoken)', outcome: 'the agent answers on the card and out loud' },
          { prompt: 'tap a quick-reply button', outcome: 'sends it back through the gateway like a real user tap' },
          { prompt: 'press the orb while it’s speaking', outcome: 'playback stops and it listens to you instead' },
        ]}
      />
      <div class="route-walkie-body">
        <WalkieTalkie />
      </div>
    </div>
  );
}
