import { render } from 'preact';
import './styles.css';
import { App } from './app';
import { SignupPage } from './components/SignupPage';

const mount = document.getElementById('app');
if (!mount) throw new Error('#app mount point missing');
render(document.querySelector('meta[name="kc-signup"]') ? <SignupPage /> : <App />, mount);
