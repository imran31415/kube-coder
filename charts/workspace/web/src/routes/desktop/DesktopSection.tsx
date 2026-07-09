import type { ComponentChildren, JSX } from 'preact';

/**
 * A labeled Desktop section: a small uppercase title, a hairline rule that
 * runs to the edge of the column, and optional right-aligned meta (a count,
 * a hint, or a small action). Gives the page a consistent, professional
 * rhythm — every block reads as a deliberate section rather than stacked
 * widgets. The header carries `data-dt-stop` so clicking a label never
 * triggers the empty-space "Add icon?" affordance.
 */
export function DesktopSection({
  title,
  icon,
  meta,
  children,
  class: klass,
  ...rest
}: {
  title: string;
  icon?: ComponentChildren;
  meta?: ComponentChildren;
  children: ComponentChildren;
  class?: string;
} & Omit<JSX.HTMLAttributes<HTMLElement>, 'title' | 'icon' | 'class'>) {
  return (
    <section class={`dt-section ${klass ?? ''}`} {...rest}>
      <header class="dt-section-head" data-dt-stop="true">
        <span class="dt-section-title">
          {icon && <span class="dt-section-icon" aria-hidden="true">{icon}</span>}
          {title}
        </span>
        <span class="dt-section-rule" aria-hidden="true" />
        {meta != null && <span class="dt-section-meta">{meta}</span>}
      </header>
      {children}
    </section>
  );
}
