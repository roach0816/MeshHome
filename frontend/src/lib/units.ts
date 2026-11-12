import type { Units } from "./api";

const FEET = 3.28084;
const MILE = 1609.344;
const INHG = 0.02953;

/** Readings in the chosen units. Stored values are always metric. */
export function unitsFor(u: Units) {
  const imp = u === "imperial";
  return {
    imperial: imp,
    temp: (c: number) => (imp ? (c * 9) / 5 + 32 : c),
    tempUnit: imp ? "°F" : "°C",
    pressure: (h: number) => (imp ? h * INHG : h),
    pressureUnit: imp ? "inHg" : "hPa",
    pressureDigits: imp ? 2 : 1,
    alt: (m: number) => (imp ? m * FEET : m),
    altUnit: imp ? "ft" : "m",
    /** Short distances (off course, a setting): feet or metres. */
    dist: (m: number) => (imp ? m * FEET : m),
    distUnit: imp ? "ft" : "m",
    /** Course distances: miles or kilometres. */
    long: (m: number) => (imp ? m / MILE : m / 1000),
    longUnit: imp ? "mi" : "km",
    /** Seconds per mile or kilometre, from metres per second. */
    pace: (mps: number) => (imp ? MILE : 1000) / mps,
  };
}

/** "3.2 mi" / "5.1 km": one decimal under 10, whole numbers above. */
export function longText(u: ReturnType<typeof unitsFor>, m: number, withUnit = true): string {
  const v = u.long(m);
  return `${v.toFixed(v < 10 ? 1 : 0)}${withUnit ? ` ${u.longUnit}` : ""}`;
}

/** "9:45 /mi" / "6:03 /km". */
export function paceText(u: ReturnType<typeof unitsFor>, mps: number): string {
  const s = Math.round(u.pace(mps));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} /${u.longUnit}`;
}
