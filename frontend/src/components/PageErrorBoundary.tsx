import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RotateCw } from "lucide-react";

/** A page that fails to render shows what went wrong instead of a blank screen. */
export class PageErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("MeshHome page error:", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="flex h-full items-center justify-center p-6" role="alert">
        <div className="max-w-md space-y-3 rounded-xl border border-line bg-surface p-5">
          <p className="flex items-center gap-2 font-semibold">
            <AlertTriangle className="size-5 text-warn" aria-hidden /> This page couldn't be shown
          </p>
          <p className="break-words font-mono text-xs text-muted">{error.message || String(error)}</p>
          <button
            type="button"
            onClick={() => location.reload()}
            className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-line bg-surface px-4 text-sm font-medium hover:bg-surface-2"
          >
            <RotateCw className="size-4" aria-hidden /> Reload
          </button>
        </div>
      </div>
    );
  }
}
