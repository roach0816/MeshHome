import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { Conversation } from "./api";
import { claimChime, playChime, soundEnabledFor, type SoundSetting } from "./sound";

export type SocketState = "connecting" | "open" | "closed";

/**
 * WebSocket events are invalidation hints only: each one triggers a REST refetch of
 * canonical state. On (re)connect everything is refetched, so missed events are harmless.
 */
export function useRealtime(enabled: boolean): SocketState {
  const qc = useQueryClient();
  const [state, setState] = useState<SocketState>("connecting");

  useEffect(() => {
    if (!enabled) return;
    let ws: WebSocket | null = null;
    let stopped = false;
    let retry = 1000;
    let timer: number | undefined;
    let poll: number | undefined;
    // The server sends at least a ping every 25 s; silence for longer means the socket died
    // (phones drop connections of apps in the background without telling them).
    let lastMessageAt = Date.now();

    type ChimeEvent = { conversation_id: string; message_id: string; kind: "dm" | "channel" };
    const maybeChime = (ev: ChimeEvent) => {
      const settings = qc.getQueryData<{ sound: SoundSetting }>(["notification-settings"]);
      const conv = qc.getQueryData<Conversation[]>(["conversations"])?.find((c) => c.id === ev.conversation_id);
      if (!soundEnabledFor(settings?.sound ?? "off", conv, ev.kind)) return;
      // No sound for the conversation you're already reading.
      const reading =
        location.pathname === `/c/${ev.conversation_id}` && document.visibilityState === "visible" && document.hasFocus();
      if (reading || !claimChime(ev.message_id)) return;
      playChime();
    };

    const connect = () => {
      setState("connecting");
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => {
        retry = 1000;
        lastMessageAt = Date.now();
        setState("open");
        qc.invalidateQueries();
      };
      ws.onmessage = (ev) => {
        lastMessageAt = Date.now();
        let msg: {
          type?: string;
          conversation_id?: string;
          message_id?: string;
          direction?: string;
          kind?: "dm" | "channel";
          suppressed?: boolean;
          key?: string;
        };
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        switch (msg.type) {
          case "message-created":
          case "delivery-updated":
            if (msg.type === "message-created" && msg.direction === "in" && !msg.suppressed && msg.message_id && msg.kind)
              maybeChime(msg as ChimeEvent);
            qc.invalidateQueries({ queryKey: ["conversations"] });
            if (msg.conversation_id) qc.invalidateQueries({ queryKey: ["messages", msg.conversation_id] });
            else qc.invalidateQueries({ queryKey: ["messages"] });
            break;
          case "read-position-updated":
          case "conversations-updated":
            qc.invalidateQueries({ queryKey: ["conversations"] });
            break;
          case "contacts-updated":
            qc.invalidateQueries({ queryKey: ["contacts"] });
            // A contact came or went: signed messages may now show as a contact's (or not).
            qc.invalidateQueries({ queryKey: ["messages"] });
            qc.invalidateQueries({ queryKey: ["my-contact"] });
            qc.invalidateQueries({ queryKey: ["map"] });
            qc.invalidateQueries({ queryKey: ["device"] });
            break;
          case "remote-updated":
            qc.invalidateQueries({ queryKey: ["remote"] });
            break;
          case "settings-updated":
            qc.invalidateQueries({ queryKey: ["notification-settings"] });
            qc.invalidateQueries({ queryKey: ["app-install"] });
            qc.invalidateQueries({ queryKey: ["bot-apps"] });
            break;
          case "radio-status-changed":
            qc.invalidateQueries({ queryKey: ["status"] });
            qc.invalidateQueries({ queryKey: ["device"] });
            break;
        }
      };
      ws.onclose = () => {
        setState("closed");
        if (stopped) return;
        timer = window.setTimeout(connect, retry);
        retry = Math.min(retry * 2, 30000);
      };
    };
    connect();
    // Light periodic resync in case an invalidation was missed while connected.
    poll = window.setInterval(() => qc.invalidateQueries({ queryKey: ["status"] }), 30000);
    // Back in the foreground (or back online): reconnect at once instead of waiting out the backoff,
    // and refetch what may have changed meanwhile (the reconnect does that too).
    const resume = () => {
      if (document.visibilityState !== "visible" || stopped) return;
      const alive = ws?.readyState === WebSocket.OPEN && Date.now() - lastMessageAt < 60000;
      if (alive) {
        qc.invalidateQueries();
        return;
      }
      if (ws?.readyState === WebSocket.CONNECTING) return;
      window.clearTimeout(timer);
      retry = 1000;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.onclose = null;
        ws.close();
      }
      connect();
    };
    const onVisible = () => resume();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", onVisible);
    window.addEventListener("pageshow", onVisible);

    return () => {
      stopped = true;
      window.clearTimeout(timer);
      window.clearInterval(poll);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", onVisible);
      window.removeEventListener("pageshow", onVisible);
      ws?.close();
    };
  }, [enabled, qc]);

  return state;
}
