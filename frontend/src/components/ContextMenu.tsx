import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { cx } from "../lib/util";

export type MenuItem =
  | {
      label: string;
      icon?: ReactNode;
      onSelect: () => void;
      danger?: boolean;
      disabled?: boolean;
    }
  | "separator";

/**
 * A positioned menu (role="menu") with keyboard support: arrows/Home/End move focus,
 * Enter/Space selects, Escape or Tab closes, and focus returns to the element that opened it.
 */
export function ContextMenu({
  x,
  y,
  items,
  label,
  onClose,
}: {
  x: number;
  y: number;
  items: MenuItem[];
  label: string;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ left: x, top: y });
  const opener = useRef<Element | null>(document.activeElement);

  // Keep the menu inside the viewport.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const { width, height } = el.getBoundingClientRect();
    const pad = 8;
    setPos({
      left: Math.max(pad, Math.min(x, window.innerWidth - width - pad)),
      top: Math.max(pad, Math.min(y, window.innerHeight - height - pad)),
    });
    el.querySelector<HTMLButtonElement>("[role=menuitem]:not(:disabled)")?.focus();
  }, [x, y]);

  useEffect(() => {
    const close = () => onClose();
    const onPointer = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) onClose();
    };
    document.addEventListener("pointerdown", onPointer, true);
    window.addEventListener("resize", close);
    window.addEventListener("blur", close);
    document.addEventListener("scroll", close, true);
    const restore = opener.current;
    return () => {
      document.removeEventListener("pointerdown", onPointer, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("blur", close);
      document.removeEventListener("scroll", close, true);
      if (restore instanceof HTMLElement && document.contains(restore)) restore.focus();
    };
  }, [onClose]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    const buttons = Array.from(
      ref.current?.querySelectorAll<HTMLButtonElement>("[role=menuitem]:not(:disabled)") ?? [],
    );
    const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const move = (n: number) => buttons[(n + buttons.length) % buttons.length]?.focus();
    if (e.key === "ArrowDown") move(i + 1);
    else if (e.key === "ArrowUp") move(i - 1);
    else if (e.key === "Home") move(0);
    else if (e.key === "End") move(buttons.length - 1);
    else if (e.key === "Escape" || e.key === "Tab") onClose();
    else return;
    e.preventDefault();
  };

  return createPortal(
    <div
      ref={ref}
      role="menu"
      aria-label={label}
      onKeyDown={onKeyDown}
      onContextMenu={(e) => e.preventDefault()}
      style={{ left: pos.left, top: pos.top, transformOrigin: `${x <= pos.left + 1 ? "left" : "right"} ${y <= pos.top + 1 ? "top" : "bottom"}` }}
      className="mc-menu fixed z-50 min-w-52 rounded-xl border border-line bg-surface p-1 shadow-xl shadow-black/10"
    >
      {items.map((item, i) =>
        item === "separator" ? (
          <div key={`sep-${i}`} role="separator" className="my-1 h-px bg-line" />
        ) : (
          <button
            key={item.label}
            role="menuitem"
            disabled={item.disabled}
            onClick={() => {
              onClose();
              item.onSelect();
            }}
            className={cx(
              "flex min-h-10 w-full items-center gap-2.5 rounded-lg px-3 text-left text-sm outline-none",
              "focus:bg-surface-2 hover:bg-surface-2 disabled:opacity-40",
              item.danger ? "text-danger" : "text-ink",
            )}
          >
            <span className="flex size-4 items-center justify-center text-muted" aria-hidden>
              {item.icon}
            </span>
            {item.label}
          </button>
        ),
      )}
    </div>,
    document.body,
  );
}
