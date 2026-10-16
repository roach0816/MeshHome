/** The browser's location, with an explanation people can act on when it is unavailable. */
export function browserLocation(): Promise<{ lat: number; lon: number; accuracy: number }> {
  return new Promise((resolve, reject) => {
    if (!window.isSecureContext) {
      reject(new Error("Browsers only share location with HTTPS pages. Open MeshHome over HTTPS, or pick the spot on the map."));
      return;
    }
    if (!("geolocation" in navigator)) {
      reject(new Error("This browser can't share its location. Pick the spot on the map instead."));
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (p) => resolve({ lat: p.coords.latitude, lon: p.coords.longitude, accuracy: p.coords.accuracy }),
      (e) =>
        reject(
          new Error(
            e.code === e.PERMISSION_DENIED
              ? "Location access was blocked. Allow it for this site in the browser's settings, or pick the spot on the map."
              : e.code === e.TIMEOUT
                ? "Finding your location took too long. Try again, or pick the spot on the map."
                : "Your location isn't available right now (location services may be off). Pick the spot on the map instead.",
          ),
        ),
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 },
    );
  });
}

/** Five decimals is about a metre: plenty for an advert, and what the fields show. */
export const fmtCoord = (v: number) => v.toFixed(5);
