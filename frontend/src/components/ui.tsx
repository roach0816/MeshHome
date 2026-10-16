import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from "react";
import { forwardRef } from "react";
import { cx } from "../lib/util";
import { InfoTip } from "./InfoTip";

type Variant = "primary" | "secondary" | "ghost" | "danger";

export function Button({
  variant = "secondary",
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      {...props}
      className={cx(
        "inline-flex min-h-11 items-center justify-center gap-2 rounded-lg px-4 text-sm font-medium transition-colors",
        "disabled:cursor-not-allowed disabled:opacity-50",
        variant === "primary" && "bg-accent text-accent-fg hover:bg-accent-hover",
        variant === "secondary" && "border border-line bg-surface text-ink hover:bg-surface-2",
        variant === "ghost" && "text-ink hover:bg-surface-2",
        variant === "danger" && "border border-line bg-surface text-danger hover:bg-surface-2",
        className,
      )}
    />
  );
}

export function IconButton({
  label,
  className,
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; children: ReactNode }) {
  return (
    <button
      {...props}
      aria-label={label}
      title={label}
      className={cx(
        "inline-flex size-11 shrink-0 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-2 hover:text-ink",
        "disabled:cursor-not-allowed disabled:opacity-40",
        className,
      )}
    >
      {children}
    </button>
  );
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...props }, ref) {
    return (
      <input
        ref={ref}
        {...props}
        className={cx(
          "min-h-11 w-full rounded-lg border border-line bg-surface px-3 text-base text-ink placeholder:text-muted sm:text-sm",
          "focus:border-accent focus:outline-none disabled:bg-surface-2 disabled:text-muted",
          className,
        )}
      />
    );
  },
);

export function Field({
  label,
  hint,
  info,
  error,
  children,
  htmlFor,
}: {
  label: string;
  hint?: ReactNode;
  /** Longer explanation, behind an (i) button next to the label. */
  info?: ReactNode;
  error?: string | null;
  children: ReactNode;
  htmlFor: string;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1">
        <label htmlFor={htmlFor} className="block text-sm font-medium text-ink">
          {label}
        </label>
        {info && <InfoTip label={`About ${label.toLowerCase()}`}>{info}</InfoTip>}
      </div>
      {children}
      {hint && !error && <p className="text-xs text-muted">{hint}</p>}
      {error && (
        <p className="text-xs text-danger" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("rounded-xl border border-line bg-surface", className)}>{children}</div>;
}

export function Badge({
  tone = "muted",
  children,
  className,
}: {
  tone?: "muted" | "ok" | "warn" | "danger" | "accent";
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4",
        tone === "muted" && "border-line text-muted",
        tone === "ok" && "border-ok/40 text-ok",
        tone === "warn" && "border-warn/40 text-warn",
        tone === "danger" && "border-danger/40 text-danger",
        tone === "accent" && "border-accent/40 bg-accent-soft text-accent",
        className,
      )}
    >
      {children}
    </span>
  );
}

export function ErrorText({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <p role="alert" className="rounded-lg border border-danger/30 bg-danger/5 px-3 py-2 text-sm text-danger">
      {msg}
    </p>
  );
}
