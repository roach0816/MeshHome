import type { Conversation } from "./api";

export type SoundSetting = "off" | "all" | "dms";

let ctx: AudioContext | null = null;

/**
 * Browsers only allow audio after a user gesture. Create/resume the AudioContext on the first
 * click or key press so later notifications can play.
 */
export function installAudioUnlock() {
  const unlock = () => {
    try {
      ctx ??= new AudioContext();
      if (ctx.state === "suspended") void ctx.resume();
    } catch {
      /* audio unavailable */
    }
  };
  for (const ev of ["pointerdown", "keydown", "touchend"]) window.addEventListener(ev, unlock, { passive: true });
}

/** A short, soft two-note chime. */
export function playChime() {
  try {
    ctx ??= new AudioContext();
    if (ctx.state === "suspended") void ctx.resume();
    const t0 = ctx.currentTime + 0.01;
    for (const [i, freq] of [880, 1318.5].entries()) {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      const start = t0 + i * 0.12;
      gain.gain.setValueAtTime(0, start);
      gain.gain.linearRampToValueAtTime(0.18, start + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.35);
      osc.connect(gain).connect(ctx.destination);
      osc.start(start);
      osc.stop(start + 0.4);
    }
  } catch {
    /* audio unavailable */
  }
}

/** Whether a new incoming message in this conversation should make a sound. */
export function soundEnabledFor(
  global: SoundSetting,
  conv: Pick<Conversation, "kind" | "sound"> | undefined,
  kind: "dm" | "channel",
): boolean {
  if (conv?.sound === "on") return true;
  if (conv?.sound === "off") return false;
  return global === "all" || (global === "dms" && kind === "dm");
}

/** Default (no override) for a conversation kind under the global setting. */
export function soundDefaultFor(global: SoundSetting, kind: "dm" | "channel"): boolean {
  return global === "all" || (global === "dms" && kind === "dm");
}

/**
 * Several open tabs all receive the same event; let only the first one chime.
 * localStorage is shared per origin, so the first tab to claim the message id wins.
 */
export function claimChime(messageId: string): boolean {
  try {
    const key = "mh.lastChime";
    if (localStorage.getItem(key) === messageId) return false;
    localStorage.setItem(key, messageId);
    return true;
  } catch {
    return true;
  }
}
