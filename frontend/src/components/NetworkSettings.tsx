import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, CircleAlert, ExternalLink, Lock, LockOpen, RefreshCw, Settings2 } from "lucide-react";
import { api, type NetworkInfo, type NetworkSnapshot, type NetworkStatus } from "../lib/api";
import { Badge, Button, Card, ErrorText, Field, Input } from "./ui";
import { Dialog } from "./Dialog";
import { cx, formatDateTime } from "../lib/util";

const ACTIVE = ["queued", "applying", "installing", "certificate", "restarting"];
const STATE_LABEL: Record<string, string> = {
  queued: "Waiting for the system helper…",
  applying: "Applying settings…",
  installing: "Installing nginx and certbot…",
  certificate: "Requesting the certificate (the DNS check takes a little while)…",
  restarting: "Restarting MeshHome…",
};

export function useNetworkInfo() {
  return useQuery({
    queryKey: ["network-info"],
    queryFn: () => api<NetworkInfo>("/api/system/network"),
    // Poll briefly while the device is still producing its first snapshot.
    refetchInterval: (q) => (q.state.data?.configurable && !q.state.data.config ? 3000 : false),
  });
}

function httpsUrl(host: string, port: number) {
  return `https://${host}${port === 443 ? "" : `:${port}`}`;
}

function addressFor(c: NetworkSnapshot) {
  return c.https_enabled && c.hostname ? httpsUrl(c.hostname, c.https_port) : `http://${location.hostname}:${c.app_port}`;
}

function certLabel(cert: NetworkInfo["certificate"], c: NetworkSnapshot) {
  if (!cert) return null;
  const issuer = cert.issuer ?? "";
  const kind = /STAGING|Fake|\(STAGING\)/i.test(issuer)
    ? "Let's Encrypt staging (not trusted by browsers)"
    : /Let's Encrypt|R1[0-9]|E[5-9]/i.test(issuer)
      ? "Let's Encrypt"
      : issuer.includes(c.hostname ?? "\u0000")
        ? "Self-signed (testing)"
        : issuer || "Certificate";
  return `${kind} · valid until ${formatDateTime(cert.not_after)}`;
}

function Row({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-1.5 text-sm">
      <dt className="shrink-0 text-muted">{k}</dt>
      <dd className="min-w-0 break-words text-right">{children}</dd>
    </div>
  );
}

/** Settings home card: a summary plus a button that opens the full configuration modal. */
export function NetworkSection() {
  const q = useNetworkInfo();
  const [open, setOpen] = useState(false);
  const d = q.data;
  const c = d?.config;

  return (
    <Card className="p-4 sm:p-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold">Network &amp; HTTPS</h3>
          <p className="mt-0.5 text-sm text-muted">How browsers reach this installation.</p>
        </div>
        {d?.configurable && (
          <Button onClick={() => setOpen(true)} disabled={!c}>
            <Settings2 className="size-4" aria-hidden /> Configure…
          </Button>
        )}
      </div>
      <div className="mt-3">
        {q.isPending && <p className="text-sm text-muted">Loading…</p>}
        <ErrorText error={q.error} />
        {d && !d.configurable && (
          <div className="space-y-2 text-sm">
            <p className="flex items-center gap-2">
              {location.protocol === "https:" ? (
                <>
                  <Lock className="size-4 text-ok" aria-hidden /> This page is served over HTTPS.
                </>
              ) : (
                <>
                  <LockOpen className="size-4 text-warn" aria-hidden /> This page is served over plain HTTP.
                </>
              )}
            </p>
            <p className="text-muted">
              In a container, your deployment handles ports and HTTPS (for example a Kubernetes Ingress with cert-manager).
            </p>
          </div>
        )}
        {d?.configurable && !c && <p className="text-sm text-muted">Reading the current configuration from the device…</p>}
        {c && (
          <dl className="divide-y divide-line">
            <Row k="Address">
              <a href={addressFor(c)} className="text-accent underline">
                {addressFor(c)}
              </a>
            </Row>
            <Row k="HTTPS">
              {c.https_enabled ? (
                <Badge tone="ok">
                  <Lock className="size-3" aria-hidden /> On · {c.hostname}
                </Badge>
              ) : (
                <Badge tone="warn">
                  <LockOpen className="size-3" aria-hidden /> Off · plain HTTP
                </Badge>
              )}
            </Row>
            {c.https_enabled && (
              <Row k="Certificate">
                {certLabel(d.certificate, c) ?? "—"}
                {c.auto_renew && <span className="block text-xs text-muted">Renews automatically</span>}
              </Row>
            )}
            <Row k="App port">
              {c.app_port} <span className="text-muted">({c.https_enabled ? "localhost only, behind nginx" : "all interfaces"})</span>
            </Row>
          </dl>
        )}
        {d?.in_progress && (
          <p className="mt-2 flex items-center gap-2 text-sm text-muted" role="status">
            <RefreshCw className="size-4 animate-spin" aria-hidden /> A settings change is being applied…
          </p>
        )}
      </div>
      {open && d && c && <NetworkDialog info={d} config={c} onClose={() => setOpen(false)} />}
    </Card>
  );
}

function Toggle({ id, checked, onChange, label, hint, disabled }: {
  id: string; checked: boolean; onChange: (v: boolean) => void; label: string; hint?: ReactNode; disabled?: boolean;
}) {
  return (
    <label htmlFor={id} className={cx("flex items-start gap-3", disabled ? "opacity-50" : "cursor-pointer")}>
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 size-5 shrink-0 accent-[var(--accent)]"
      />
      <span>
        <span className="block text-sm font-medium">{label}</span>
        {hint && <span className="block text-xs text-muted">{hint}</span>}
      </span>
    </label>
  );
}

function Group({ title, children, description }: { title: string; children: ReactNode; description?: ReactNode }) {
  return (
    <section className="space-y-3 border-t border-line pt-4 first:border-t-0 first:pt-0">
      <div>
        <h3 className="text-sm font-semibold">{title}</h3>
        {description && <p className="text-xs text-muted">{description}</p>}
      </div>
      {children}
    </section>
  );
}

type Form = {
  app_port: string;
  https_enabled: boolean;
  hostname: string;
  https_port: string;
  redirect_http: boolean;
  email: string;
  staging: boolean;
  propagation_seconds: string;
  dns_provider: string;
  credentials: Record<string, string>; // write-only; blank keeps a saved value
};

const FQDN = /^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;

function NetworkDialog({ info, config, onClose }: { info: NetworkInfo; config: NetworkSnapshot; onClose: () => void }) {
  const qc = useQueryClient();
  const [step, setStep] = useState<"edit" | "review" | "progress">(info.in_progress ? "progress" : "edit");
  const [f, setF] = useState<Form>({
    app_port: String(config.app_port),
    https_enabled: config.https_enabled,
    hostname: config.hostname ?? "",
    https_port: String(config.https_port),
    redirect_http: config.redirect_http,
    email: config.email ?? "",
    staging: config.staging,
    propagation_seconds: String(config.propagation_seconds),
    dns_provider: config.dns_provider || "cloudflare",
    credentials: {},
  });
  const provider = info.providers.find((p) => p.id === f.dns_provider);
  const credsSaved = config.credentials_provider === f.dns_provider;
  const setCred = (env: string, v: string) => setF((x) => ({ ...x, credentials: { ...x.credentials, [env]: v } }));
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF((x) => ({ ...x, [k]: v }));
  const submittedAt = useRef(0);

  const host = f.hostname.trim().toLowerCase();
  const appPort = Number(f.app_port);
  const httpsPort = Number(f.https_port);
  const prop = Number(f.propagation_seconds);
  const errors: Partial<Record<keyof Form | string, string>> = {};
  if (!Number.isInteger(appPort) || appPort < 1024 || appPort > 65535) errors.app_port = "1024–65535 (the app runs unprivileged)";
  if (f.https_enabled) {
    if (!FQDN.test(host)) errors.hostname = "A full hostname, e.g. meshcore.example.com";
    if (!Number.isInteger(httpsPort) || httpsPort < 1 || httpsPort > 65535) errors.https_port = "1–65535";
    else if (httpsPort === appPort) errors.https_port = "Must differ from the app port";
    else if (f.redirect_http && (httpsPort === 80 || appPort === 80)) errors.https_port = "Port 80 is used by the redirect";
    if (f.email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(f.email.trim())) errors.email = "Enter a valid email or leave it blank";
    if (!provider) errors.dns_provider = "Choose your DNS provider";
    for (const field of provider?.fields ?? []) {
      const v = (f.credentials[field.env] ?? "").trim();
      if (!v && field.required && !field.default && !credsSaved) errors[field.env] = `Enter the ${field.label}`;
      if (v && field.kind === "json") {
        try {
          JSON.parse(v);
        } catch {
          errors[field.env] = "This is not valid JSON";
        }
      }
      if (v && field.kind !== "json" && (v.length > 500 || /['\u0000-\u001f]/.test(v))) errors[field.env] = "Unexpected characters";
    }
    if (!Number.isInteger(prop) || (prop !== 0 && (prop < 10 || prop > 600))) errors.propagation_seconds = "0 (automatic) or 10–600";
  }
  const valid = Object.keys(errors).length === 0;

  const newUrl = f.https_enabled ? httpsUrl(host, httpsPort) : `http://${location.hostname}:${appPort}`;
  const changes = useMemo(() => {
    const list: string[] = [];
    if (f.https_enabled) {
      const newCreds = Object.values(f.credentials).some((v) => v.trim());
      const name = provider?.name ?? f.dns_provider;
      if (!config.https_packages_installed) list.push("Install nginx from Debian's own repositories");
      if (config.acme_client !== "lego") list.push("Download lego, the Let's Encrypt client (MIT licence), from GitHub and check its SHA-256");
      if (config.acme_client === "certbot")
        list.push("Move certificate renewals from certbot to lego (your saved Cloudflare token is reused)");
      if (newCreds || !credsSaved) list.push(`Save the ${name} credentials (readable by root only; never shown again)`);
      const needsCert =
        !config.https_enabled || host !== config.hostname || f.staging !== config.staging || newCreds ||
        f.dns_provider !== config.dns_provider || config.acme_client !== "lego";
      if (needsCert)
        list.push(`Request a ${f.staging ? "staging (untrusted, for testing) " : ""}certificate for ${host} from Let's Encrypt via ${name}`);
      list.push(`Serve HTTPS on port ${httpsPort}${f.redirect_http ? " and redirect port 80 to it" : ""}, with nginx`);
      if (!config.https_enabled) list.push(`Make the app listen on 127.0.0.1:${appPort} only, behind nginx`);
    } else if (config.https_enabled) {
      list.push("Turn HTTPS off: remove the nginx site and stop renewals (the certificate and saved credentials are kept)");
      list.push(`Serve the app over plain HTTP on port ${appPort}, on all network interfaces`);
    }
    if (appPort !== config.app_port) list.push(`Move the app from port ${config.app_port} to ${appPort}`);
    if (f.https_enabled === config.https_enabled && appPort === config.app_port && list.length === 0)
      list.push("Update the saved certificate settings");
    return list;
  }, [f, config, host, appPort, httpsPort, provider, credsSaved]);
  const restarts = appPort !== config.app_port || f.https_enabled !== config.https_enabled;
  const addressChanges = newUrl !== addressFor(config);

  const apply = useMutation({
    mutationFn: () =>
      api("/api/system/network", {
        method: "PUT",
        json: {
          app_port: appPort,
          https_enabled: f.https_enabled,
          hostname: f.https_enabled ? host : null,
          https_port: httpsPort,
          redirect_http: f.redirect_http,
          email: f.email.trim() || null,
          staging: f.staging,
          propagation_seconds: prop,
          dns_provider: f.dns_provider,
          credentials: Object.fromEntries(Object.entries(f.credentials).filter(([, v]) => v.trim())),
        },
      }),
    onMutate: () => {
      submittedAt.current = Date.now() / 1000;
    },
    onSuccess: () => {
      setF((x) => ({ ...x, credentials: {} })); // don't keep secrets in memory longer than needed
      setStep("progress");
    },
  });
  const renew = useMutation({
    mutationFn: () => api("/api/system/network/renew", { method: "POST" }),
    onMutate: () => {
      submittedAt.current = Date.now() / 1000;
    },
    onSuccess: () => setStep("progress"),
  });

  const close = () => {
    qc.invalidateQueries({ queryKey: ["network-info"] });
    onClose();
  };

  return (
    <Dialog
      size="lg"
      title={step === "review" ? "Review changes" : step === "progress" ? "Applying network settings" : "Network & HTTPS"}
      onClose={close}
      footer={
        step === "edit" ? (
          <>
            <Button variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button variant="primary" disabled={!valid} onClick={() => setStep("review")}>
              Review changes
            </Button>
          </>
        ) : step === "review" ? (
          <>
            <Button variant="ghost" onClick={() => setStep("edit")}>
              Back
            </Button>
            <Button variant="primary" onClick={() => apply.mutate()} disabled={apply.isPending}>
              {apply.isPending ? "Sending…" : "Apply changes"}
            </Button>
          </>
        ) : undefined
      }
    >
      {step === "edit" && (
        <div className="space-y-5">
          <Group title="Web server" description="The port MeshHome itself listens on.">
            <Field label="App port" htmlFor="net-port" error={errors.app_port} hint={f.https_enabled ? "Only reachable from this device; nginx forwards HTTPS to it." : "Browsers connect to this port directly."}>
              <Input id="net-port" inputMode="numeric" className="max-w-40" value={f.app_port} onChange={(e) => set("app_port", e.target.value)} />
            </Field>
          </Group>

          <Group title="HTTPS" description="Encrypts the connection with a trusted certificate. Recommended.">
            <Toggle id="net-https" checked={f.https_enabled} onChange={(v) => set("https_enabled", v)} label="Serve over HTTPS" hint="nginx terminates HTTPS in front of the app." />
            {f.https_enabled && (
              <div className="grid gap-3 sm:grid-cols-[1fr_8rem]">
                <Field label="Hostname" htmlFor="net-host" error={errors.hostname} hint="In a domain your DNS provider manages, and resolving to this device on your network.">
                  <Input id="net-host" placeholder="meshcore.example.com" autoCapitalize="none" spellCheck={false} value={f.hostname} onChange={(e) => set("hostname", e.target.value)} />
                </Field>
                <Field label="HTTPS port" htmlFor="net-hport" error={errors.https_port}>
                  <Input id="net-hport" inputMode="numeric" value={f.https_port} onChange={(e) => set("https_port", e.target.value)} />
                </Field>
              </div>
            )}
            {f.https_enabled && (
              <Toggle id="net-redirect" checked={f.redirect_http} onChange={(v) => set("redirect_http", v)} label="Redirect HTTP (port 80) to HTTPS" />
            )}
          </Group>

          {f.https_enabled && (
            <Group title="Certificate" description="Issued by Let's Encrypt and renewed automatically (lego).">
              <Field label="Email for expiry notices (optional)" htmlFor="net-email" error={errors.email}>
                <Input id="net-email" type="email" autoCapitalize="none" value={f.email} onChange={(e) => set("email", e.target.value)} />
              </Field>
              <Field
                label="DNS provider"
                htmlFor="net-dns"
                error={errors.dns_provider}
                hint="Where your domain's DNS is hosted. A temporary DNS record proves you control the domain, so this device never needs to be reachable from the internet."
              >
                <select
                  id="net-dns"
                  value={f.dns_provider}
                  onChange={(e) => setF((x) => ({ ...x, dns_provider: e.target.value, credentials: {} }))}
                  className="min-h-11 w-full rounded-lg border border-line bg-surface px-3 text-base text-ink sm:text-sm"
                >
                  {info.providers.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </Field>
              {provider && (
                <div className="space-y-3 rounded-lg border border-line p-3">
                  <p className="text-xs text-muted">
                    {provider.help}{" "}
                    <a href={provider.docs} target="_blank" rel="noopener noreferrer" className="whitespace-nowrap text-accent underline">
                      Details <ExternalLink className="inline size-3" aria-hidden />
                    </a>
                  </p>
                  {provider.note && <p className="rounded-md bg-warn/10 px-2.5 py-1.5 text-xs">{provider.note}</p>}
                  {provider.fields.map((field) => {
                    const id = `net-cred-${field.env}`;
                    const keep = credsSaved ? "Saved — leave blank to keep" : undefined;
                    const value = f.credentials[field.env] ?? "";
                    return (
                      <Field key={field.env} label={field.label} htmlFor={id} error={errors[field.env]}>
                        {field.kind === "json" ? (
                          <textarea
                            id={id}
                            rows={4}
                            spellCheck={false}
                            placeholder={keep ?? "Paste the whole JSON key file"}
                            value={value}
                            onChange={(e) => setCred(field.env, e.target.value)}
                            className="w-full rounded-lg border border-line bg-surface px-3 py-2 font-mono text-xs text-ink placeholder:text-muted focus:border-accent focus:outline-none"
                          />
                        ) : field.kind === "choice" ? (
                          <select
                            id={id}
                            value={value || field.default || ""}
                            onChange={(e) => setCred(field.env, e.target.value)}
                            className="min-h-11 w-full rounded-lg border border-line bg-surface px-3 text-base text-ink sm:text-sm"
                          >
                            {(field.choices ?? []).map((c) => (
                              <option key={c} value={c}>
                                {c}
                              </option>
                            ))}
                          </select>
                        ) : (
                          <Input
                            id={id}
                            type={field.secret ? "password" : "text"}
                            autoComplete="off"
                            autoCapitalize="none"
                            spellCheck={false}
                            placeholder={keep ?? field.default ?? ""}
                            value={value}
                            onChange={(e) => setCred(field.env, e.target.value)}
                          />
                        )}
                      </Field>
                    );
                  })}
                  <p className="text-xs text-muted">Stored on the device for root only and never shown again.</p>
                </div>
              )}
              <details className="text-xs text-muted">
                <summary className="cursor-pointer text-ink">My DNS provider isn't listed</summary>
                <p className="mt-1.5">
                  Some registrars (for example Squarespace, Wix or Bluehost) have no DNS API. Hand just the validation to a
                  free provider that has one: create a free account at deSEC (desec.io) with a domain such as
                  <code className="mx-1 font-mono">yourname.dedyn.io</code>, then at your registrar add one CNAME record:
                  <code className="mx-1 font-mono">_acme-challenge.{host || "meshcore.example.com"}</code>→
                  <code className="mx-1 font-mono">_acme-challenge.yourname.dedyn.io</code>. Choose deSEC above with its
                  token. lego follows the CNAME, and your own DNS stays where it is.
                </p>
              </details>
              {config.acme_client === "certbot" && (
                <p className="rounded-md bg-surface-2 px-3 py-2 text-xs">
                  This device still renews its certificate with certbot. Saving moves it to lego and reuses the saved
                  Cloudflare token; nothing else changes.
                </p>
              )}
              <div className="grid gap-3 sm:grid-cols-[10rem_1fr]">
                <Field label="DNS wait (seconds)" htmlFor="net-prop" error={errors.propagation_seconds} hint="0 = check automatically (recommended).">
                  <Input id="net-prop" inputMode="numeric" value={f.propagation_seconds} onChange={(e) => set("propagation_seconds", e.target.value)} />
                </Field>
                <div className="pt-7">
                  <Toggle id="net-staging" checked={f.staging} onChange={(v) => set("staging", v)} label="Use Let's Encrypt staging" hint="For testing only: browsers will not trust the certificate." />
                </div>
              </div>
              {config.https_enabled && (
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg bg-surface-2 px-3 py-2 text-sm">
                  <span>
                    {certLabel(info.certificate, config) ?? "No certificate yet"}
                    {config.auto_renew && (
                      <span className="block text-xs text-muted">
                        {config.acme_client === "certbot" ? "certbot" : "lego"} renews it automatically before it expires
                      </span>
                    )}
                  </span>
                  <Button onClick={() => renew.mutate()} disabled={renew.isPending}>
                    <RefreshCw className="size-4" aria-hidden /> Renew now
                  </Button>
                </div>
              )}
            </Group>
          )}
          <ErrorText error={renew.error} />
        </div>
      )}

      {step === "review" && (
        <div className="space-y-3 text-sm">
          <p>These changes will be made on the device:</p>
          <ul className="list-disc space-y-1.5 pl-5">
            {changes.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
          {restarts && <p className="text-muted">MeshHome restarts once; this page reconnects by itself.</p>}
          {addressChanges && (
            <p className="rounded-lg bg-warn/10 px-3 py-2">
              Afterwards, open MeshHome at <strong>{newUrl}</strong>
              {f.https_enabled && " (make sure that name points to this device on your network)"}.
            </p>
          )}
          <p className="text-muted">If anything fails, the previous configuration is restored automatically.</p>
          <ErrorText error={apply.error} />
        </div>
      )}

      {step === "progress" && <Progress since={submittedAt.current} newUrl={newUrl} addressChanges={addressChanges} onDone={close} />}
    </Dialog>
  );
}

function Progress({ since, newUrl, addressChanges, onDone }: { since: number; newUrl: string; addressChanges: boolean; onDone: () => void }) {
  const qc = useQueryClient();
  const [offlineSince, setOfflineSince] = useState<number | null>(null);
  const q = useQuery({
    queryKey: ["network-status"],
    queryFn: async () => {
      try {
        const r = await api<{ status: NetworkStatus | null; in_progress: boolean }>("/api/system/network/status");
        setOfflineSince(null);
        return r;
      } catch (e) {
        setOfflineSince((t) => t ?? Date.now());
        throw e;
      }
    },
    refetchInterval: 2000,
    retry: false,
  });
  // Ignore a status left over from an earlier change.
  const st = q.data?.status && q.data.status.updated_at >= since - 5 ? q.data.status : null;
  const done = st?.state === "done";
  const failed = st?.state === "failed";
  useEffect(() => {
    if (done || failed) qc.invalidateQueries({ queryKey: ["network-info"] });
  }, [done, failed, qc]);
  const longOffline = offlineSince !== null && Date.now() - offlineSince > 15000;

  return (
    <div className="space-y-3 text-sm" aria-live="polite">
      {done ? (
        <>
          <p className="flex items-center gap-2 text-ok">
            <CheckCircle2 className="size-5" aria-hidden /> {st.message}
          </p>
          <div className="flex flex-wrap gap-2">
            {addressChanges && (
              <a href={newUrl} className="inline-flex min-h-11 items-center gap-2 rounded-lg bg-accent px-4 font-medium text-accent-fg">
                Open {newUrl}
              </a>
            )}
            <Button onClick={onDone}>Close</Button>
          </div>
        </>
      ) : failed ? (
        <>
          <p className="flex items-start gap-2 text-danger">
            <CircleAlert className="mt-0.5 size-5 shrink-0" aria-hidden /> {st.message}
          </p>
          {st.log_tail && st.log_tail.length > 0 && (
            <details className="rounded-lg bg-surface-2 px-3 py-2 text-xs" open>
              <summary className="cursor-pointer text-muted">Details</summary>
              <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-snug">{st.log_tail.join("\n")}</pre>
            </details>
          )}
          <Button onClick={onDone}>Close</Button>
        </>
      ) : (
        <>
          <p className="flex items-center gap-2">
            <RefreshCw className="size-4 animate-spin text-accent" aria-hidden />
            {offlineSince ? "MeshHome is restarting — reconnecting…" : st ? STATE_LABEL[st.state] ?? st.message : "Waiting for the system helper…"}
          </p>
          {!offlineSince && st?.message && ACTIVE.includes(st.state) && <p className="text-muted">{st.message}</p>}
          {longOffline && addressChanges && (
            <p className="rounded-lg bg-warn/10 px-3 py-2">
              This page can't reach the old address any more. Continue at{" "}
              <a href={newUrl} className="font-medium text-accent underline">
                {newUrl}
              </a>
              .
            </p>
          )}
        </>
      )}
    </div>
  );
}
