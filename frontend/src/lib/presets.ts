import type { RadioPreset } from "./api";

export const BANDWIDTHS = [7.8, 10.4, 15.6, 20.8, 31.25, 41.7, 62.5, 125, 250, 500];
export const CUSTOM = "custom";

const sameRf = (p: RadioPreset, f: number, bw: number, sf: number, cr: number) =>
  Math.abs(p.freq_mhz - f) < 0.0005 && Math.abs(p.bw_khz - bw) < 0.01 && p.sf === sf && p.cr === cr;

/** The preset describing these settings: same RF, preferring one whose path hash size also matches. */
export function matchPreset(list: RadioPreset[], f: number, bw: number, sf: number, cr: number, hashSize: number | null) {
  const rf = list.filter((p) => sameRf(p, f, bw, sf, cr));
  return rf.find((p) => (p.path_hash_size ?? 1) === (hashSize ?? 1)) ?? rf.find((p) => p.path_hash_size === null) ?? rf[0];
}
