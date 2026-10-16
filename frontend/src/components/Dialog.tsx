import { useEffect, useLayoutEffect, useRef, type ReactNode } from "react";
import { X } from "lucide-react";
import { IconButton } from "./ui";

/** Modal built on the native <dialog>: focus containment, Escape and inert background for free. */
export function Dialog({
  title,
  onClose,
  children,
  footer,
  size = "md",
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg";
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (el && !el.open) el.showModal();
    return () => el?.close();
  }, []);
  // Exit animation for every way a dialog can close (X, Escape, backdrop, or the parent simply
  // un-rendering it): just before React removes the node, leave an inert visual copy that fades
  // out. Layout-effect cleanup runs while the node is still in the DOM.
  useLayoutEffect(() => {
    const opened = performance.now();
    return () => {
      const el = ref.current;
      // Skip React StrictMode's immediate mount/unmount probe and reduced-motion users.
      if (!el || performance.now() - opened < 80 || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      const ghost = document.createElement("div");
      ghost.className = "mc-dialog-ghost";
      ghost.setAttribute("aria-hidden", "true");
      const copy = el.cloneNode(true) as HTMLDialogElement;
      copy.removeAttribute("id");
      copy.querySelectorAll("[id]").forEach((n) => n.removeAttribute("id"));
      copy.setAttribute("open", "");
      copy.inert = true;
      ghost.append(copy);
      document.body.append(ghost);
      const done = () => ghost.remove();
      ghost.addEventListener("animationend", (e) => e.target === ghost && done());
      setTimeout(done, 400); // safety net
    };
  }, []);
  return (
    <dialog
      ref={ref}
      aria-labelledby="dialog-title"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onClick={(e) => {
        if (e.target === ref.current) onClose(); // backdrop click
      }}
      className={`mc-dialog m-auto w-[calc(100%-2rem)] ${size === "sm" ? "max-w-sm" : size === "lg" ? "max-w-2xl" : "max-w-lg"} rounded-2xl border border-line bg-surface p-0 text-ink shadow-2xl backdrop:bg-black/40 backdrop:backdrop-blur-[2px]`}
    >
      <div className="flex max-h-[85dvh] flex-col">
        <header className="flex items-center justify-between gap-2 border-b border-line py-1.5 pl-5 pr-2">
          <h2 id="dialog-title" className="truncate text-base font-semibold">
            {title}
          </h2>
          <IconButton label="Close" onClick={onClose}>
            <X className="size-5" />
          </IconButton>
        </header>
        <div className="min-h-0 overflow-y-auto px-5 py-4">{children}</div>
        {footer && <footer className="flex justify-end gap-2 border-t border-line px-5 py-3">{footer}</footer>}
      </div>
    </dialog>
  );
}
