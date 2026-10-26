export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** Headers a state-changing request from the browser needs (for requests not made with api()). */
export const REQUESTED_WITH = "meshhome";

export function writeHeaders(): Record<string, string> {
  const h: Record<string, string> = { "X-Requested-With": REQUESTED_WITH };
  const token = csrfToken();
  if (token) h["X-CSRF-Token"] = token;
  return h;
}

/** The message of a failed response's {"detail": ...}, as api() reports it. */
export function errorDetail(body: unknown): string {
  return describe((body as { detail?: unknown } | null)?.detail);
}

function csrfToken(): string {
  // mch_csrf: a sign-in from before the rename to MeshHome, still valid.
  const m = document.cookie.match(/(?:^|;\s*)mh_csrf=([^;]+)/) ?? document.cookie.match(/(?:^|;\s*)mch_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

function describe(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => (typeof d?.msg === "string" ? d.msg.replace(/^Value error, /, "") : "Invalid input"))
      .join("; ");
  }
  return "Request failed";
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, headers, ...rest } = init;
  const method = (rest.method ?? (json !== undefined ? "POST" : "GET")).toUpperCase();
  const h = new Headers(headers);
  if (method !== "GET") {
    h.set("X-Requested-With", REQUESTED_WITH);
    const token = csrfToken();
    if (token) h.set("X-CSRF-Token", token);
  }
  if (json !== undefined) h.set("Content-Type", "application/json");
  const res = await fetch(path, {
    ...rest,
    method,
    headers: h,
    credentials: "same-origin",
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });
  if (res.status === 204) return undefined as T;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, describe((data as { detail?: unknown }).detail));
  return data as T;
}

// ---- types (mirror backend schemas) ---------------------------------------------------

export type RadioMode = "simulated" | "tcp" | "hat" | "none";

export interface RadioConfig {
  mode: RadioMode;
  host: string;
  port: number;
  paused: boolean;
  sim_interval_seconds: number;
}

export interface Me {
  username: string;
  home_name: string;
}

/** A signed-in browser (Account → Signed-in devices). */
export interface SignedInDevice {
  id: string;
  user_agent: string | null;
  created_at: string;
  last_seen_at: string;
  expires_at: string;
  current: boolean;
}

export type MessageState =
  | "received"
  | "queued"
  | "sending"
  | "accepted"
  | "acknowledged"
  | "no_ack"
  | "uncertain"
  | "failed"
  | "expired";

export interface Message {
  id: string;
  position: number;
  conversation_id: string;
  direction: "in" | "out";
  sender_label: string | null;
  sender_key_prefix: string | null;
  body: string;
  sender_timestamp: number | null;
  created_at: string;
  state: MessageState;
  error: string | null;
  duplicate_count: number;
  is_simulated: boolean;
  client_message_id: string | null;
  meta: Record<string, unknown>;
  /** Signed channel messages: how the signature checks out (null: not signed). */
  signature?: MessageSignature | null;
}

/** self: sent by this radio; contact: signed by one of the radio's contacts; known: valid, but the
 *  signer isn't a contact; unknown: signed by a key this radio hasn't heard yet; invalid: doesn't match. */
export type SignatureState = "self" | "contact" | "known" | "unknown" | "invalid";

export interface MessageSignature {
  state: SignatureState;
  key_id: string;
  signer_name: string | null;
  signer_key: string | null;
}

/** This radio's own contact, for sharing (GET /api/contacts/me). */
export interface MyContact {
  name: string;
  public_key: string;
  kind: number;
  uri: string;
}

export interface Conversation {
  id: string;
  kind: "channel" | "dm";
  title: string;
  favorite: boolean;
  muted: boolean;
  sound: "on" | "off" | null;
  blocked: boolean;
  last_message_at: string | null;
  last_position: number;
  read_position: number;
  unread: number;
  is_simulated: boolean;
  archived: boolean;
  channel_slot: number | null;
  contact_id: string | null;
  contact_public_key: string | null;
  peer_prefix: string | null;
  max_bytes: number;
  sign_messages?: boolean;
  contacts_only?: boolean;
  preview: {
    body: string;
    direction: "in" | "out";
    sender_label: string | null;
    state: MessageState;
    created_at: string;
  } | null;
}

export interface MessagePage {
  messages: Message[];
  has_more: boolean;
}

export interface Contact {
  id: string;
  public_key: string;
  name: string;
  alias: string | null;
  kind: number;
  last_advert_at: string | null;
  on_radio: boolean;
  favorite: boolean;
  blocked: boolean;
  is_simulated: boolean;
  conversation_id: string | null;
}

export interface ContactPage {
  items: Contact[];
  total: number;
  page: number;
  page_size: number;
}

export interface ContactDetail extends Contact {
  lat: number | null;
  lon: number | null;
  path_len: number; // -1 flood, 0 direct, n hops
  path_hops: string[];
  path_hash_size: number;
  messages_received: number;
  messages_sent: number;
}

export interface RadioStatus {
  state:
    | "starting"
    | "disabled"
    | "not_configured"
    | "paused"
    | "connecting"
    | "connected"
    | "backoff"
    | "lock_unavailable";
  detail: string;
  mode: RadioMode;
  is_simulated: boolean;
  radio_name: string | null;
  connected_since: number | null;
  last_interaction_at: number | null;
  last_error: string | null;
  next_retry_at: number | null;
  reconnects: number;
  received: number;
  sent: number;
  storage_warning: string | null;
}

export interface Status {
  app: { version: string; radio_enabled_env: boolean };
  database: { ok: boolean; messages: number };
  radio: RadioStatus;
  radio_config: { mode: RadioMode; host: string; port: number; paused: boolean };
  realtime_clients: number;
  gaps: { started_at: string; ended_at: string | null; reason: string; open: boolean }[];
  server_time: number;
}

export interface Device {
  radio: {
    id: string;
    name: string;
    public_key: string;
    is_simulated: boolean;
    device_info: Record<string, unknown>;
    rf: Record<string, number | null>;
    last_connected_at: string | null;
    live: boolean;
  } | null;
  channels: { slot: number; name: string; generation: number; active: boolean }[];
  contacts: number;
}

export interface SearchHit extends Message {
  conversation_title: string;
}

export interface ConversationInfo {
  conversation: Conversation;
  created_at: string;
  radio_name: string;
  contact: {
    id: string;
    name: string;
    alias: string | null;
    public_key: string;
    kind: number;
    last_advert_at: string | null;
    on_radio: boolean;
  } | null;
  channel: { slot: number; name: string; generation: number; active: boolean; flood_scope: string | null } | null;
  stats: {
    total: number;
    incoming: number;
    outgoing: number;
    first_message_at: string | null;
    last_message_at: string | null;
  };
  delete_action: "clear" | "delete";
}

export interface MapConfig {
  tile_url: string;
  attribution: string;
  max_zoom: number;
  /** Name the place at a node's position, looked up by the server (OpenStreetMap Nominatim). */
  place_names: boolean;
}

/** GET /api/settings/app-install: "Install as an app" (HTTPS only). */
export interface AppInstall {
  enabled: boolean;
  /** This page reached the server over HTTPS (needed to turn it on). */
  https: boolean;
}

/** GET /api/places: the place at a position, e.g. "Springfield, Illinois, US". */
export interface Place {
  name: string | null;
  enabled: boolean;
  source: string;
}

/** A contact matching a key prefix (GET /api/contacts/lookup). */
export interface ContactMatch extends Contact {
  lat: number | null;
  lon: number | null;
  /** From your radio's advertised position, when both are known. */
  distance_km: number | null;
}

export interface ContactLookup {
  prefix: string;
  items: ContactMatch[];
}

export interface MapNode {
  id: string;
  public_key: string;
  name: string;
  alias: string | null;
  kind: number;
  lat: number;
  lon: number;
  last_advert_at: string | null;
  on_radio: boolean;
  is_simulated: boolean;
  conversation_id: string | null;
}

export interface MapData {
  gateways: { name: string; lat: number; lon: number; is_simulated: boolean; live: boolean }[];
  nodes: MapNode[];
  without_location: number;
  tiles: MapConfig;
}

export interface SetupStatus {
  needs_setup: boolean;
  version: string;
  release_url: string | null;
  radio_hat_ready?: boolean;
}

export interface RadioHatInfo {
  available: boolean;
  unavailable_reason: string | null;
  phase: "absent" | "checking" | "installing" | "rebooting" | "needs_reboot" | "ready" | "stopped";
  model: string | null;
  board: "pi4" | "pi5" | null;
  hat_product: string | null;
  spi_device: boolean;
  service: { installed?: boolean; active?: boolean; state?: string; restarts?: number | null; condition_met?: boolean };
  installed_version: string | null;
  pinned_version: string | null;
  last_message: string | null;
  last_state: string | null;
  updated_at: number | null;
  host: string;
  port: number;
  request_pending: boolean;
}

export type ChannelKeyKind = "none" | "public" | "hashtag" | "private";
export type TelemetryMode = 0 | 1 | 2;

export interface NodeConfig {
  simulated: boolean;
  firmware: { version_code: number; version: string | null; build: string | null; model: string | null };
  identity: { name: string; lat: number | null; lon: number | null; share_location: boolean };
  radio: {
    freq_mhz: number;
    bw_khz: number;
    sf: number;
    cr: number;
    tx_power_dbm: number;
    max_tx_power_dbm: number | null;
    repeat: boolean | null;
  };
  behavior: {
    auto_add_contacts: boolean;
    multi_acks: number;
    path_hash_mode: number | null;
    default_flood_scope: string | null;
  };
  telemetry: { base: TelemetryMode; location: TelemetryMode; environment: TelemetryMode };
  tuning: { rx_delay: number; airtime_factor: number } | null;
  channels: { slot: number; name: string; key: ChannelKeyKind; flood_scope?: string | null }[];
  max_channels: number;
  custom_vars: Record<string, string> | null;
}

export type UpdateState = "queued" | "downloading" | "installing" | "migrating" | "restarting" | "done" | "failed" | "rolled_back";

export interface UpdateStatus {
  state: UpdateState;
  version: string;
  message: string;
  log_tail?: string[];
  updated_at: number;
}

export interface UpdateInfo {
  current_version: string;
  install_kind: "native" | "container";
  checks_enabled: boolean;
  checked_at: number | null;
  error: string | null;
  latest: { version: string; url: string; notes: string; published_at: string | null; has_native_package: boolean } | null;
  installed: { version: string; url: string; notes: string; published_at: string | null } | null;
  update_available: boolean;
  can_install: boolean;
  command?: string;
  legacy_layout?: boolean;
  status: UpdateStatus | null;
}

export interface NetworkSnapshot {
  app_port: number;
  bind: string;
  https_enabled: boolean;
  hostname: string | null;
  https_port: number;
  redirect_http: boolean;
  email: string | null;
  staging: boolean;
  dns_provider: string;
  credentials_provider: string | null; // credentials are saved for this provider (never shown)
  acme_client: "lego" | "certbot" | null;
  propagation_seconds: number; // 0 = automatic
  token_saved: boolean;
  https_packages_installed: boolean;
  auto_renew: boolean;
  updated_at: number;
}

export interface NetworkStatus {
  state: "queued" | "applying" | "installing" | "certificate" | "restarting" | "done" | "failed";
  message: string;
  log_tail?: string[];
  updated_at: number;
}

export interface NetworkInfo {
  install_kind: "native" | "container";
  configurable: boolean;
  config: NetworkSnapshot | null;
  certificate: { host: string; not_after: string; issuer?: string } | null;
  status: NetworkStatus | null;
  in_progress: boolean;
  providers: DnsProvider[];
}

export interface DnsProviderField {
  env: string;
  label: string;
  secret: boolean;
  required: boolean;
  kind?: "text" | "choice" | "json";
  default?: string;
  choices?: string[];
}

export interface DnsProvider {
  id: string;
  name: string;
  lego: string;
  fields: DnsProviderField[];
  help: string;
  note: string | null;
  docs: string;
}

export type ApiKeyScope = "read" | "write";

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  scope: ApiKeyScope;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  expired: boolean;
}

export interface ApiKeyCreated {
  key: string;
  api_key: ApiKey;
}

export interface RadioPreset {
  id: string;
  title: string;
  freq_mhz: number;
  bw_khz: number;
  sf: number;
  cr: number;
  path_hash_size: number | null;
}

export interface PresetList {
  source: "live" | "bundled";
  updated: string | null;
  info_message: string | null;
  presets: RadioPreset[];
}

// ---- remote administration (repeaters, room servers) ----------------------------------

export type RemoteKind = "status" | "telemetry" | "acl" | "neighbours" | "owner" | "regions";

export interface RemoteSection<T = Record<string, unknown>> {
  data: T;
  at: string;
}

export interface RemoteStatus {
  bat: number;
  tx_queue_len: number;
  noise_floor: number;
  last_rssi: number;
  nb_recv: number;
  nb_sent: number;
  airtime: number;
  uptime: number;
  sent_flood: number;
  sent_direct: number;
  recv_flood: number;
  recv_direct: number;
  full_evts: number;
  last_snr: number;
  direct_dups: number;
  flood_dups: number;
  rx_airtime: number;
  recv_errors: number;
}

export interface RemoteNeighbours {
  neighbours_count: number;
  results_count: number;
  neighbours: { pubkey: string; secs_ago: number; snr: number }[];
}

export interface RemoteState {
  contact: { id: string; name: string; public_key: string; kind: number; lat: number | null; lon: number | null };
  simulated: boolean;
  radio_connected: boolean;
  session: { admin: boolean; permissions: number | null; at: string } | null;
  busy: boolean;
  names: Record<string, string>;
  sections: {
    status?: RemoteSection<RemoteStatus>;
    telemetry?: RemoteSection<{ lpp: { channel: number; type: string; value: unknown }[] }>;
    acl?: RemoteSection<{ acl: { key: string; perm: number }[] }>;
    neighbours?: RemoteSection<RemoteNeighbours>;
    owner?: RemoteSection<{ text: string }>;
    regions?: RemoteSection<{ text: string }>;
  };
  values: Record<string, { value: string; at: string }>;
  console: { at: string; dir: "out" | "in" | "note"; text: string }[];
}


// ---- one message: details, sender, paths ----------------------------------------------

export interface ContactBrief {
  id: string;
  name: string;
  alias: string | null;
  public_key: string;
  kind: number;
  last_advert_at: string | null;
  on_radio: boolean;
  favorite: boolean;
  blocked: boolean;
}

export interface MessagePath {
  hops: { hash: string; names: string[] }[];
  hash_size: number | null;
  route: "flood" | "direct" | null;
  snr: number | null;
  rssi: number | null;
}

export interface MessageInfo {
  message: Message;
  conversation_kind: "dm" | "channel";
  sender: { label: string | null; key_prefix: string | null; contact: ContactBrief | null; match: "key" | "name" | null };
  received: {
    snr: number | null;
    rssi: number | null;
    route: "flood" | "direct" | null;
    hops: number | null;
    path_hash_size: number | null;
  };
  paths: MessagePath[];
}

// ---- bot apps (Settings → Bot → Apps) ----------------------------------------------------------

export interface BotAppOption {
  value: string;
  label: string;
}

export interface BotAppSetting {
  key: string;
  label: string;
  /** groups: "name: id, id" per line. select: one of `options`, or of `options_by[<depends_on's value>]`. */
  type: "host" | "text" | "secret" | "groups" | "select";
  required: boolean;
  placeholder: string;
  help: string;
  options?: BotAppOption[] | null;
  depends_on?: string | null;
  options_by?: Record<string, BotAppOption[]> | null;
  /** select: chosen while nothing is saved. */
  default?: string | null;
}

export interface BotAppInfo {
  id: string;
  name: string;
  version: string;
  summary: string;
  /** Catalog category, e.g. "Weather" ("Other" when an app has none). */
  category: string;
  description: string;
  author: string;
  homepage: string;
  /** group: the groups setting a command reads; it's sent as "/<name> <group>". */
  commands: { name: string; help: string; group?: string | null }[];
  settings: BotAppSetting[];
  /** What it talks to, as phrases: "contacts api.example.com", "receives data from a device on your local network". */
  contacts: string[];
}

export interface BotAppCatalogEntry extends BotAppInfo {
  installed: boolean;
  compatible: boolean;
  min_meshhome: string;
}

export interface BotAppInstalled extends BotAppInfo {
  enabled: boolean;
  values: Record<string, string>;
  secrets_set: string[];
  update: { version: string; changes: string[] } | null;
  /** Apps whose data a device sends to MeshHome (a push source). */
  push?: { path: string; help: string; format: string; max_age: number; received_at: string | null } | null;
  /** Why it needs settings before it can answer (e.g. an update changed a list of choices). */
  problem?: string | null;
}

export interface BotAppsState {
  installed: BotAppInstalled[];
  catalog: BotAppCatalogEntry[];
  catalog_source: "online" | "bundled";
  catalog_error: string | null;
  /** The catalog's categories, from its apps. */
  categories: string[];
}

// ---- sensors attached to the Pi (GET /api/sensors) ----------------------------------------------

export interface SensorsConfig {
  environment: boolean;
  gnss: boolean;
  gnss_device: string;
  update_location: boolean;
  location_min_move_m: number;
  units: "metric" | "imperial";
}

export interface EnvReading {
  temperature_c: number;
  humidity: number | null;
  pressure_hpa: number | null;
  gas_ohms: number | null;
  at: number;
}

export interface GnssReading {
  fix?: boolean;
  lat?: number;
  lon?: number;
  alt_m?: number;
  satellites?: number | null;
  hdop?: number;
  utc?: string;
  at?: number;
  fix_at?: number;
}

export interface SensorsState {
  environment: { status: "off" | "not_found" | "ok" | "error"; address: number | null; reading: EnvReading | null };
  gnss: {
    status: "off" | "not_found" | "silent" | "no_fix" | "fix" | "error";
    device: string | null;
    reading: GnssReading | null;
    location_update: { at: number; lat: number; lon: number; moved_m: number | null } | null;
  };
  config: SensorsConfig;
}

export interface SensorHistory {
  hours: number;
  points: { at: string; temperature_c: number | null; humidity: number | null; pressure_hpa: number | null; gas_ohms: number | null }[];
}
