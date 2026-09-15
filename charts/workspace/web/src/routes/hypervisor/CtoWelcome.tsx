import { ClaudeCredentialSetup } from '../../components/ClaudeCredentialSetup';
import { claudeProbed, claudeReady, refreshClaudeReady } from '../../store/claude';
import { sending, sendMessage } from '../../store/hypervisor';
import './ctoWelcome.css';

/**
 * The AI CTO's opening beat, rendered into Chat's empty state when the next new
 * chat is in CTO mode (#683).
 *
 * This is everything that was worth keeping from the /cto page's hero: a
 * deterministic opener, starter chips that cost nothing until tapped, the
 * first-win build prompts a just-onboarded user sees (#487), and the
 * Claude-credential gate that stops a keyless user firing a build that can only
 * die on a raw provider error (#494).
 *
 * It goes through <Chat>'s `welcome` slot rather than above it, so opener,
 * chips and composer read as one centred hero (#500).
 */

/** Deterministic starter chips — no LLM call fires until one is tapped. */
function starterChips(projectName: string | null): string[] {
  return [
    'What should I focus on?',
    projectName ? `Where is ${projectName} at?` : 'What are we building?',
    'Break a goal into tasks',
  ];
}

/** First-win starter chips (#487) — build-first prompts for a just-onboarded
 *  user. Tapping one sends a one-sentence build request that the #486 fast-path
 *  builds immediately, with the #484/#485 preview auto-surfacing. */
const FIRST_WIN_CHIPS = ['Portfolio site', 'To-do app', 'Landing page'];

export function CtoWelcome({
  projectName,
  firstWin = false,
}: {
  /** The project the next chat will be filed into, if any — named in the
   *  opener and in one of the starter chips. */
  projectName: string | null;
  /** True for the one visit straight out of onboarding (#487). */
  firstWin?: boolean;
}) {
  const chips = firstWin ? FIRST_WIN_CHIPS : starterChips(projectName);
  const lead = firstWin
    ? "Welcome aboard — I'm your AI CTO. Tell me in one sentence what you'd like to build, and I'll get started right away."
    : projectName
      ? `I'm across ${projectName}. What do you want to move on?`
      : "I already know your workspace. What do you want to build or move on?";

  // Keyless chip-flash (#500): the connect gate keys off `claudeReady === false`,
  // but the signal starts `null`, so a keyless user would see live build chips
  // for the length of the probe — tapping one in that window fires a build that
  // dies on a raw provider error. The chips stay inert until the first probe
  // *settles*; `claudeProbed` (not `claudeReady !== null`) is the condition, so
  // a failed probe — which deliberately leaves the value unknown — releases them
  // rather than disabling them forever.
  const chipsPending = !claudeProbed.value;

  if (claudeReady.value === false) {
    return (
      <div class="cto-welcome">
        <p class="cto-welcome-lead">
          Connect Claude and I'll start building. Sign in with your Claude
          subscription, or paste an API key.
        </p>
        <div class="cto-connect">
          <ClaudeCredentialSetup
            ready={claudeReady.value}
            onConnected={() => void refreshClaudeReady()}
          />
        </div>
      </div>
    );
  }

  return (
    <div class="cto-welcome">
      <p class="cto-welcome-lead">{lead}</p>
      <div class="cto-chips" aria-busy={chipsPending}>
        {chips.map((c) => (
          <button
            key={c}
            type="button"
            class="cto-chip"
            disabled={sending.value || chipsPending}
            onClick={() => void sendMessage(c)}
          >
            {c}
          </button>
        ))}
      </div>
    </div>
  );
}
