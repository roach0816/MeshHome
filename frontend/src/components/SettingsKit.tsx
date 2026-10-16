import type { ReactNode } from "react";
import { InfoTip } from "./InfoTip";
import { Card } from "./ui";

export function Section({
  title,
  description,
  info,
  actions,
  children,
}: {
  title: string;
  /** One short line under the title. */
  description?: ReactNode;
  /** The longer explanation, behind an (i) button next to the title. */
  info?: ReactNode;
  /** Buttons at the right of the title. */
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Card className="p-4 sm:p-5">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3 className="flex items-center gap-1 text-base font-semibold">
            {title}
            {info && <InfoTip label={`About ${title.toLowerCase()}`}>{info}</InfoTip>}
          </h3>
          {description && <p className="mt-0.5 text-sm text-muted">{description}</p>}
        </div>
        {actions && <div className="flex shrink-0 flex-wrap gap-2">{actions}</div>}
      </div>
      <div className="mt-4 space-y-4">{children}</div>
    </Card>
  );
}

export function Select<T extends string | number>({
  id,
  value,
  options,
  onChange,
  disabled,
}: {
  id: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
  disabled?: boolean;
}) {
  return (
    <select
      id={id}
      disabled={disabled}
      value={String(value)}
      onChange={(e) => {
        const o = options.find((x) => String(x.value) === e.target.value);
        if (o) onChange(o.value);
      }}
      className="min-h-11 w-full rounded-lg border border-line bg-surface px-3 text-base text-ink focus:border-accent focus:outline-none disabled:bg-surface-2 disabled:text-muted sm:text-sm"
    >
      {options.map((o) => (
        <option key={String(o.value)} value={String(o.value)}>
          {o.label}
        </option>
      ))}
    </select>
  );
}
