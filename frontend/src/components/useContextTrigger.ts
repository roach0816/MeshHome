import { useRef } from "react";

const LONG_PRESS_MS = 500;

/**
 * Props that open a context menu from: right-click, long-press on touch screens (iOS Safari never
 * fires contextmenu on links), and the keyboard (Menu key / Shift+F10).
 */
export function useContextTrigger() {
  const press = useRef<{ timer: number; x: number; y: number; fired: boolean } | null>(null);
  const cancel = () => {
    if (press.current) window.clearTimeout(press.current.timer);
  };

  return (open: (x: number, y: number) => void) => ({
    onContextMenu: (e: React.MouseEvent<HTMLElement>) => {
      e.preventDefault();
      if (e.clientX === 0 && e.clientY === 0) {
        const r = e.currentTarget.getBoundingClientRect();
        open(r.left + 48, r.top + r.height / 2);
      } else open(e.clientX, e.clientY);
    },
    onKeyDown: (e: React.KeyboardEvent<HTMLElement>) => {
      if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) {
        e.preventDefault();
        const r = e.currentTarget.getBoundingClientRect();
        open(r.left + 48, r.top + r.height / 2);
      }
    },
    onTouchStart: (e: React.TouchEvent) => {
      const t = e.touches[0];
      cancel();
      press.current = {
        x: t.clientX,
        y: t.clientY,
        fired: false,
        timer: window.setTimeout(() => {
          if (press.current) press.current.fired = true;
          navigator.vibrate?.(10);
          open(t.clientX, t.clientY);
        }, LONG_PRESS_MS),
      };
    },
    onTouchMove: (e: React.TouchEvent) => {
      const t = e.touches[0];
      if (press.current && Math.hypot(t.clientX - press.current.x, t.clientY - press.current.y) > 10) cancel();
    },
    onTouchEnd: (e: React.TouchEvent) => {
      cancel();
      if (press.current?.fired) e.preventDefault(); // don't also activate the row
    },
  });
}
