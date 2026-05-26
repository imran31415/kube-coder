import { render } from 'preact';
import './styles.css';
import { App } from './app';

const mount = document.getElementById('app');
if (!mount) throw new Error('#app mount point missing');
render(<App />, mount);
