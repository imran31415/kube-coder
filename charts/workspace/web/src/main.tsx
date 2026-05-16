import { render } from 'preact';
import './styles/reset.css';
import './styles/tokens.css';
import './styles/globals.css';
import { App } from './app';

const mount = document.getElementById('app');
if (!mount) throw new Error('#app mount point missing');
render(<App />, mount);
