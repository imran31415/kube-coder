import { render } from 'preact';
import './styles/reset.css';
import './styles/tokens.css';
import './styles/globals.css';
import { App } from './app';
import { loadServerMode } from './store/server-mode';

// Probe deployment mode (read-only public demo vs. authed personal
// workspace) before first paint of any mutation UI. Fire-and-forget — the
// signal defaults to a writable shape, so a slow probe just briefly shows
// the buttons that the public demo will hide once the response lands.
loadServerMode();

const mount = document.getElementById('app');
if (!mount) throw new Error('#app mount point missing');
render(<App />, mount);
