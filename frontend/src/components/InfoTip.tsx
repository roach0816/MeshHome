import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Info } from "lucide-react";
import { cx } from "../lib/util";

/**
 * A small (i) button with more detail: shown on hover or focus with a mouse, and on tap on touch
 * screens. Escape or a tap elsewhere closes it. For explanations that would otherwise be a paragraph
 * under a setting.
 */
export function InfoTip({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false); // opened by click/tap: stays until dismissed
  const [alignRight, setAlignRight] = useState(false);
  const wrap = useRef<HTMLSpanElement>(null);
  const id = useId();

  const show = () => {
    const r = wrap.current?.getBoundingClientRect();
    // Keep the panel (18rem) on screen: open it leftwards near the right edge.
    if (r) setAlignRight(r.left + 288 > window.innerWidth - 16);
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (!wrap.current?.contains(e.target as Node)) {
        setOpen(false);
        setPinned(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        setPinned(false);
      }
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <span
      ref={wrap}
      className={cx("relative inline-flex align-middle", className)}
      onPointerEnter={(e) => e.pointerType === "mouse" && show()}
      onPointerLeave={(e) => e.pointerType === "mouse" && !pinned && setOpen(false)}
    >
      <button
        type="button"
        aria-label={label}
        aria-expanded={open}
        aria-controls={id}
        onClick={(e) => {
          e.preventDefault(); // inside a <label>: don't toggle its control
          e.stopPropagation();
          if (open && pinned) {
            setOpen(false);
            setPinned(false);
          } else {
            show();
            setPinned(true);
          }
        }}
        onFocus={show}
        onBlur={() => !pinned && setOpen(false)}
        className="inline-flex size-6 items-center justify-center rounded-full text-muted hover:bg-surface-2 hover:text-ink focus-visible:outline-2 focus-visible:outline-accent"
      >
        <Info className="size-4" aria-hidden />
      </button>
      {open && (
        <span
          id={id}
          role="note"
          className={cx(
            "absolute top-full z-40 mt-1 block w-72 max-w-[calc(100vw-2rem)] rounded-lg border border-line bg-surface p-3 text-left text-xs font-normal leading-relaxed text-ink shadow-lg",
            "[&_a]:text-accent [&_a]:underline [&_p+p]:mt-2",
            alignRight ? "right-0" : "left-0",
          )}
        >
          {children}
        </span>
      )}
    </span>
  );
}
