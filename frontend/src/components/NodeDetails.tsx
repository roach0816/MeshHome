import { useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router";
import { MessageSquare, SlidersHorizontal } from "lucide-react";
import { api, type ContactLookup, type ContactMatch, type Place } from "../lib/api";
import { formatDateTime } from "../lib/util";
import { Badge, Button, ErrorText } from "./ui";

const KIND: Record<number, string> = { 1: "Companion", 2: "Repeater", 3: "Room server", 4: "Sensor" };

function Row({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[7rem_1fr] gap-3 py-1.5 text-sm">
      <dt className="text-muted">{k}</dt>
      <dd className="min-w-0 break-words">{children ?? "—"}</dd>
    </div>
  );
}

function distance(km: number): string {
  return km < 1 ? `${Math.round(km * 1000)} m` : `${km < 10 ? km.toFixed(1) : Math.round(km)} km`;
}

/** "Springfield, Illinois, US" for a position, looked up by the server (Settings → Map). */
function PlaceName({ lat, lon }: { lat: number; lon: number }) {
  const place = useQuery({
    queryKey: ["place", lat.toFixed(3), lon.toFixed(3)],
    queryFn: () => api<Place>(`/api/places?lat=${lat}&lon=${lon}`),
    staleTime: Infinity,
  });
  if (place.isPending) return <span className="block text-xs text-muted">Looking up the place…</span>;
  if (!place.data?.enabled || !place.data.name) return null;
  return <span className="block">{place.data.name}</span>;
}

function NodeCard({ c, onNavigate }: { c: ContactMatch; onNavigate?: () => void }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [error, setError] = useState<unknown>(null);
  const go = (to: string) => {
    onNavigate?.();
    navigate(to);
  };
  const message = async () => {
    setError(null);
    try {
      const r = await api<{ conversation_id: string }>(`/api/contacts/${c.id}/conversation`, { method: "POST" });
      qc.invalidateQueries({ queryKey: ["conversations"] });
      go(`/c/${r.conversation_id}`);
    } catch (e) {
      setError(e);
    }
  };
  const located = c.lat !== null && c.lon !== null;
  return (
    <div className="rounded-lg border border-line p-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="mr-1 min-w-0 break-words font-medium">{c.alias || c.name || "Unnamed"}</span>
        <Badge tone="accent">{KIND[c.kind] ?? `Type ${c.kind}`}</Badge>
        {c.blocked && <Badge tone="danger">Blocked</Badge>}
        {!c.on_radio && <Badge>Removed from radio</Badge>}
        {c.is_simulated && <Badge tone="warn">Simulated</Badge>}
      </div>
      <dl className="mt-1 divide-y divide-line">
        {c.alias && <Row k="Advertised name">{c.name || "—"}</Row>}
        <Row k="Public key">
          <code className="break-all font-mono text-xs">{c.public_key}</code>
        </Row>
        <Row k="Last heard">{c.last_advert_at ? formatDateTime(c.last_advert_at) : "Never"}</Row>
        <Row k="Location">
          {located ? (
            <>
              <PlaceName lat={c.lat!} lon={c.lon!} />
              <Link to="/map" className="text-accent underline" onClick={onNavigate}>
                {c.lat!.toFixed(5)}, {c.lon!.toFixed(5)}
              </Link>
            </>
          ) : (
            "Not shared"
          )}
        </Row>
        {c.distance_km !== null && <Row k="Distance">{distance(c.distance_km)} from your radio</Row>}
      </dl>
      {c.kind >= 1 && c.kind <= 3 && (
      <div className="mt-2 flex flex-wrap gap-2">
        {(c.kind === 2 || c.kind === 3) && (
          <Button onClick={() => go(`/contacts/${c.id}/manage`)}>
            <SlidersHorizontal className="size-4" aria-hidden /> Remote manage
          </Button>
        )}
        {c.kind === 1 && (
          <Button onClick={() => void message()}>
            <MessageSquare className="size-4" aria-hidden /> Send message
          </Button>
        )}
      </div>
      )}
      <ErrorText error={error} />
    </div>
  );
}

/**
 * Who a key prefix is: a hop in a message's path, or a repeater's neighbour. Prefixes are short,
 * so several contacts can match, and a node whose advert your radio hasn't heard matches none.
 */
export function NodeDetails({ prefix, onNavigate }: { prefix: string; onNavigate?: () => void }) {
  const lookup = useQuery({
    queryKey: ["contact-lookup", prefix.toLowerCase()],
    queryFn: () => api<ContactLookup>(`/api/contacts/lookup?prefix=${encodeURIComponent(prefix)}`),
  });
  const items = lookup.data?.items ?? [];
  return (
    <div className="space-y-3">
      {lookup.isPending && <p className="text-sm text-muted">Loading…</p>}
      <ErrorText error={lookup.error} />
      {lookup.data && items.length === 0 && (
        <p className="text-sm">
          No contact's key starts with <code className="font-mono">{prefix}</code>. Your radio hasn't heard this node's advert
          yet (or it was removed from your contacts), so there's nothing more to show.
        </p>
      )}
      {items.length > 1 && (
        <p className="text-sm text-muted">
          {items.length} of your contacts have keys starting with <code className="font-mono">{prefix}</code>, so this could be
          any of them. Repeaters are listed first.
        </p>
      )}
      {items.map((c) => (
        <NodeCard key={c.id} c={c} onNavigate={onNavigate} />
      ))}
    </div>
  );
}
