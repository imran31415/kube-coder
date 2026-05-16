import type { ComponentChildren, JSX } from 'preact';
import './Button.css';

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger';
type Size = 'sm' | 'md' | 'lg';

export interface ButtonProps extends Omit<JSX.HTMLAttributes<HTMLButtonElement>, 'size'> {
  variant?: Variant;
  size?: Size;
  iconOnly?: boolean;
  children?: ComponentChildren;
}

export function Button({ variant = 'secondary', size = 'md', iconOnly = false, class: klass, className, ...rest }: ButtonProps) {
  const classes = [
    'btn',
    `btn-${variant}`,
    `btn-${size}`,
    iconOnly ? 'btn-icon' : '',
    klass || className || '',
  ]
    .filter(Boolean)
    .join(' ');
  return <button type="button" class={classes} {...rest} />;
}
