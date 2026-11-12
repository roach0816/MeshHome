import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, type TrackingConfig } from "../lib/api";
import { useTrackingSettings } from "../pages/Shell";
import { ErrorText, Field } from "./ui";
import { Section, Select } from "./SettingsKit";

const TRAILS = [0, 15, 30, 60, 120, 240, 720, 1440];

function trailLabel(m: number) {
  if (m === 0) return "No trail";
  return m < 60 ? `Last ${m} minutes` : `Last ${m / 60} hour${m === 60 ? "" : "s"}`;
}

export function TrackingSection() {
  const qc = useQueryClient();
  const cfg = useTrackingSettings();
  const save = useMutation({
    mutationFn: (body: TrackingConfig) => api<TrackingConfig>("/api/settings/tracking", { method: "PUT", json: body }),
    onSuccess: (v) => {
      qc.setQueryData(["tracking-settings"], v);
      qc.invalidateQueries({ queryKey: ["tracking"] });
    },
  });
  const c = save.isPending && save.variables ? save.variables : cfg.data;
  return (
    <Section
      title="Tracking"
      description="Follow GPS trackers on a map of their own, with your course drawn on it."
      info={
        <>
          <p>
            MeshCore trackers don't send their position on a timer, so MeshHome asks: on each tracker's interval it sends a
            telemetry request, and the tracker answers with its latest GPS fix and battery. Positions from its adverts are
            recorded too. Each request is two short packets, repeated by each repeater on the way.
          </p>
          <p>
            When a request on the stored route goes unanswered (the runner moved), MeshHome forgets the route and asks again by
            flood; the reply teaches the radio the new route. Positions are kept for 30 days.
          </p>
          <p>
            On each tracker, in the MeshCore app: turn GPS on, set Telemetry → Location to allow this node (flag it in the
            tracker's contacts) or everyone, and make sure this node is in its contacts.
          </p>
        </>
      }
    >
      {c ? (
        <>
          <label className="flex cursor-pointer items-start gap-3">
            <input
              type="checkbox"
              checked={c.enabled}
              disabled={save.isPending}
              onChange={(e) => save.mutate({ ...c, enabled: e.target.checked })}
              className="mt-0.5 size-5 shrink-0 accent-[var(--accent)]"
            />
            <span>
              <span className="block text-sm font-medium">Turn on tracking</span>
              <span className="block text-xs text-muted">Adds Tracking to the navigation. Off: nothing is asked for.</span>
            </span>
          </label>
          {c.enabled && (
            <>
              <Field label="Units" htmlFor="trk-units" hint="Distances, pace, altitude, temperature and pressure on the Tracking page.">
                <Select
                  id="trk-units"
                  value={c.units}
                  options={[
                    { value: "metric" as const, label: "Metric (km, m, °C, hPa)" },
                    { value: "imperial" as const, label: "Imperial (mi, ft, °F, inHg)" },
                  ]}
                  onChange={(v) => save.mutate({ ...c, units: v })}
                />
              </Field>
              <Field label="Trail on the map" htmlFor="trk-trail" hint="The recent path drawn behind each tracker.">
                <Select
                  id="trk-trail"
                  value={c.trail_minutes}
                  disabled={save.isPending}
                  options={[...new Set([...TRAILS, c.trail_minutes])].sort((a, b) => a - b).map((m) => ({ value: m, label: trailLabel(m) }))}
                  onChange={(v) => save.mutate({ ...c, trail_minutes: v })}
                />
              </Field>
              <label className="flex cursor-pointer items-start gap-3">
                <input
                  type="checkbox"
                  checked={c.back_off}
                  disabled={save.isPending}
                  onChange={(e) => save.mutate({ ...c, back_off: e.target.checked })}
                  className="mt-0.5 size-5 shrink-0 accent-[var(--accent)]"
                />
                <span>
                  <span className="block text-sm font-medium">Ask less often when a tracker doesn't answer</span>
                  <span className="block text-xs text-muted">
                    After 3 unanswered requests, up to every 5 minutes until it answers: kinder to a busy shared mesh. Turn off at
                    an event on your own mesh to keep asking at each tracker's interval.
                  </span>
                </span>
              </label>
              <p className="text-sm">
                <Link to="/tracking" className="text-accent underline">
                  Open the tracking map
                </Link>{" "}
                to add trackers and courses. For a venue without internet, save an offline map in{" "}
                <Link to="/settings/map" className="text-accent underline">
                  Settings → Map
                </Link>
                .
              </p>
            </>
          )}
        </>
      ) : (
        <ErrorText error={cfg.error} />
      )}
      <ErrorText error={save.error} />
    </Section>
  );
}
