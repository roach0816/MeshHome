/**
 * MeshCore channel share links, as used by the MeshCore app's QR codes:
 *   meshcore://channel/add?name=<name>&secret=<32 hex>[&region_scope=<scope>]
 * See https://docs.meshcore.io/qr_codes/
 */

export const PUBLIC_KEY_HEX = "8b3387e9c5cdea6ac9e5edbaa115cd72";

export type ChannelLink = { name: string; secret: string; scope: string };

export function channelLink({ name, secret, scope }: ChannelLink): string {
  // Encode every reserved character (including spaces as %20) so any QR reader round-trips it.
  let url = `meshcore://channel/add?name=${encodeURIComponent(name)}&secret=${secret.toLowerCase()}`;
  if (scope) url += `&region_scope=${encodeURIComponent(scope)}`;
  return url;
}

export type ParsedLink =
  | { kind: "channel"; link: ChannelLink }
  | { kind: "contact"; name: string }
  | { kind: "invalid"; reason: string };

export function parseLink(text: string): ParsedLink {
  const t = text.trim();
  const m = /^meshcore:\/\/(channel|contact)\/add\?(.*)$/i.exec(t);
  if (!m) return { kind: "invalid", reason: "This QR code is not a MeshCore channel." };
  const params = new URLSearchParams(m[2]);
  if (m[1].toLowerCase() === "contact") return { kind: "contact", name: params.get("name") ?? "" };
  const name = (params.get("name") ?? "").trim();
  const secret = (params.get("secret") ?? "").trim().toLowerCase();
  if (!name) return { kind: "invalid", reason: "The channel link has no name." };
  if (!/^[0-9a-f]{32}$/.test(secret)) return { kind: "invalid", reason: "The channel link has no valid key." };
  const scope = (params.get("region_scope") ?? "").trim().replace(/^#/, "");
  return { kind: "channel", link: { name, secret, scope } };
}

/** Normalise a region scope as typed ("#boulder" or "boulder"); "" means no scope. */
export const normaliseScope = (s: string) => s.trim().replace(/^#+/, "");

export const scopeError = (s: string): string | null => {
  const v = normaliseScope(s);
  if (/\s/.test(v)) return "Scope names cannot contain spaces";
  if (new TextEncoder().encode(v).length > 30) return "At most 30 bytes";
  return null;
};
