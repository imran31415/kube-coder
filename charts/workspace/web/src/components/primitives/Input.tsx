import type { JSX } from 'preact';
import './Input.css';

export interface InputProps extends JSX.HTMLAttributes<HTMLInputElement> {
  fullWidth?: boolean;
}

export function Input({ fullWidth, class: klass, className, ...rest }: InputProps) {
  return <input class={['input', fullWidth ? 'input-full' : '', klass || className || ''].filter(Boolean).join(' ')} {...rest} />;
}
