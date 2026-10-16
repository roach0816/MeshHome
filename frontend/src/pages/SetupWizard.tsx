import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { ArchiveRestore, Check, Cpu, FlaskConical, KeyRound, Radio, RadioTower, UserRound, WifiOff } from "lucide-react";
import { api, type Me, type RadioMode } from "../lib/api";
import { Button, Card, ErrorText, Field, Input } from "../components/ui";
import { cx } from "../lib/util";
import { useSetupStatus } from "../lib/queries";
import { RestoreFlow } from "../components/BackupRestore";

const STEPS = [
  { title: "Verify", icon: KeyRound },
  { title: "Owner account", icon: UserRound },
  { title: "Radio", icon: RadioTower },
  { title: "Review", icon: Check },
];

const HAT_MODE = {
  mode: "hat" as RadioMode,
  title: "Radio HAT on this Pi",
  body: "The RAK6421 LoRa HAT that the installer set up on this Raspberry Pi.",
  icon: Cpu,
};

const MODES: { mode: RadioMode; title: string; body: string; icon: typeof Radio }[] = [
  {
    mode: "simulated",
    title: "Simulated radio",
    body: "No hardware yet. A built-in simulator produces labelled sample traffic so you can try everything.",
    icon: FlaskConical,
  },
  {
    mode: "tcp",
    title: "MeshCore Ethernet companion",
    body: "Connect to a MeshCore companion radio over TCP on your network (e.g. a RAK4631 Ethernet build).",
    icon: RadioTower,
  },
  {
    mode: "none",
    title: "Decide later",
    body: "Finish setup without a radio. History and settings are available; connect a radio any time.",
    icon: WifiOff,
  },
];

export function SetupWizard() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [token, setToken] = useState("");
  const [homeName, setHomeName] = useState("Home");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [mode, setMode] = useState<RadioMode>("simulated");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("5000");
  const [touched, setTouched] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const setupStatus = useSetupStatus();
  const hatReady = !!setupStatus.data?.radio_hat_ready;
  const modes = hatReady ? [HAT_MODE, ...MODES] : MODES;
  // The installer already set up a radio HAT: offer it first.
  useEffect(() => {
    if (hatReady) setMode("hat");
  }, [hatReady]);

  const pwError =
    password.length > 0 && password.length < 10
      ? "Use at least 10 characters"
      : confirm.length > 0 && confirm !== password
        ? "Passwords do not match"
        : null;
  const accountValid = !!homeName.trim() && /^[A-Za-z0-9_.@-]+$/.test(username) && password.length >= 10 && password === confirm;
  const portNum = Number(port);
  const radioValid =
    mode !== "tcp" || (!!host.trim() && !/[\s/]/.test(host.trim()) && Number.isInteger(portNum) && portNum > 0 && portNum < 65536);

  const submit = useMutation({
    mutationFn: () =>
      api<Me>("/api/setup", {
        json: {
          setup_token: token.trim(),
          username,
          password,
          home_name: homeName.trim(),
          radio: { mode, host: mode === "tcp" ? host.trim() : "", port: portNum || 5000, sim_interval_seconds: 60 },
        },
      }),
    onSuccess: async (me) => {
      qc.setQueryData(["me"], me);
      await qc.invalidateQueries({ queryKey: ["setup-status"] });
      navigate("/", { replace: true });
    },
    onError: (err) => {
      if (err.message.toLowerCase().includes("setup token")) setStep(0);
    },
  });

  const tokenRejected = !!submit.error?.message.toLowerCase().includes("setup token");

  const next = (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (step === 0 && !token.trim()) return;
    if (step === 1 && !accountValid) return;
    if (step === 2 && !radioValid) return;
    setTouched(false);
    if (step < 3) setStep(step + 1);
    else submit.mutate();
  };

  return (
    <div className="min-h-dvh bg-bg px-4 py-8 sm:py-14">
      <div className="mx-auto w-full max-w-lg">
        <div className="mb-6 flex items-center gap-3">
          <img src="/favicon.svg" alt="" className="size-10" />
          <div>
            <h1 className="text-xl font-semibold">
              Set up Mesh<span className="text-accent">Home</span>
            </h1>
            <p className="text-sm text-muted">Takes about a minute. Everything stays on your server.</p>
          </div>
        </div>

        <ol className="mb-5 grid grid-cols-4 gap-2" aria-label="Setup progress">
          {STEPS.map((s, i) => (
            <li key={s.title} className="space-y-1.5" aria-current={i === step ? "step" : undefined}>
              <div className={cx("h-1 rounded-full", i <= step ? "bg-accent" : "bg-line")} />
              <span className={cx("block truncate text-xs", i === step ? "font-medium text-ink" : "text-muted")}>
                {i + 1}. {s.title}
              </span>
            </li>
          ))}
        </ol>

        <Card className="p-5 sm:p-6">
          <form onSubmit={next} className="space-y-5" noValidate>
            {step === 0 && (
              <>
                <div className="space-y-2">
                  <h2 className="text-lg font-semibold">Confirm you own this server</h2>
                  <p className="text-sm text-muted">
                    The server printed a one-time <strong className="text-ink">setup token</strong> in its log when it
                    started. This stops anyone else on your network from claiming the app first.
                  </p>
                  <pre className="overflow-x-auto rounded-lg bg-surface-2 px-3 py-2 text-xs text-muted">
                    docker compose logs app | grep -A3 "setup token"
                  </pre>
                </div>
                <Field
                  label="Setup token"
                  htmlFor="token"
                  error={
                    touched && !token.trim()
                      ? "Enter the token from the server log"
                      : tokenRejected
                        ? submit.error?.message
                        : null
                  }
                >
                  <Input
                    id="token"
                    autoFocus
                    autoComplete="off"
                    spellCheck={false}
                    value={token}
                    onChange={(e) => {
                      setToken(e.target.value);
                      submit.reset();
                    }}
                    className="font-mono"
                  />
                </Field>
              </>
            )}

            {step === 1 && (
              <>
                <div className="space-y-1">
                  <h2 className="text-lg font-semibold">Create the owner account</h2>
                  <p className="text-sm text-muted">You'll use this to sign in from any browser on your network.</p>
                </div>
                <Field label="Name for this inbox" htmlFor="home" hint="Shown in the app header.">
                  <Input id="home" value={homeName} maxLength={64} onChange={(e) => setHomeName(e.target.value)} />
                </Field>
                <Field
                  label="Username"
                  htmlFor="username"
                  error={touched && !/^[A-Za-z0-9_.@-]+$/.test(username) ? "Letters, numbers and . _ @ - only" : null}
                >
                  <Input
                    id="username"
                    autoComplete="username"
                    autoCapitalize="none"
                    spellCheck={false}
                    maxLength={64}
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                  />
                </Field>
                <Field label="Password" htmlFor="pw" hint="At least 10 characters. A passphrase works well.">
                  <Input
                    id="pw"
                    type="password"
                    autoComplete="new-password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                </Field>
                <Field label="Confirm password" htmlFor="pw2" error={pwError}>
                  <Input
                    id="pw2"
                    type="password"
                    autoComplete="new-password"
                    value={confirm}
                    onChange={(e) => setConfirm(e.target.value)}
                  />
                </Field>
              </>
            )}

            {step === 2 && (
              <>
                <div className="space-y-1">
                  <h2 className="text-lg font-semibold">Connect your home radio</h2>
                  <p className="text-sm text-muted">You can change this at any time in Settings.</p>
                </div>
                <fieldset className="space-y-2">
                  <legend className="sr-only">Radio mode</legend>
                  {modes.map((m) => (
                    <label
                      key={m.mode}
                      className={cx(
                        "flex cursor-pointer gap-3 rounded-lg border p-3 transition-colors",
                        mode === m.mode ? "border-accent bg-accent-soft/40" : "border-line hover:bg-surface-2",
                      )}
                    >
                      <input
                        type="radio"
                        name="mode"
                        value={m.mode}
                        checked={mode === m.mode}
                        onChange={() => setMode(m.mode)}
                        className="mt-1 accent-[var(--accent)]"
                      />
                      <m.icon className="mt-0.5 size-5 shrink-0 text-accent" aria-hidden />
                      <span>
                        <span className="block text-sm font-medium">{m.title}</span>
                        <span className="block text-xs text-muted">{m.body}</span>
                      </span>
                    </label>
                  ))}
                </fieldset>
                {mode === "tcp" && (
                  <div className="grid grid-cols-[1fr_6.5rem] gap-3">
                    <Field
                      label="Radio IP address or hostname"
                      htmlFor="host"
                      error={touched && !radioValid ? "Enter an address like 192.168.1.50" : null}
                    >
                      <Input
                        id="host"
                        inputMode="url"
                        autoCapitalize="none"
                        spellCheck={false}
                        placeholder="192.168.1.50"
                        value={host}
                        onChange={(e) => setHost(e.target.value)}
                      />
                    </Field>
                    <Field label="TCP port" htmlFor="port">
                      <Input id="port" inputMode="numeric" value={port} onChange={(e) => setPort(e.target.value)} />
                    </Field>
                  </div>
                )}
              </>
            )}

            {step === 3 && (
              <>
                <h2 className="text-lg font-semibold">Review</h2>
                <dl className="divide-y divide-line rounded-lg border border-line text-sm">
                  {[
                    ["Inbox name", homeName],
                    ["Owner", username],
                    [
                      "Radio",
                      mode === "tcp"
                        ? `MeshCore companion at ${host.trim()}:${portNum}`
                        : modes.find((m) => m.mode === mode)!.title,
                    ],
                  ].map(([k, v]) => (
                    <div key={k} className="flex justify-between gap-4 px-3 py-2.5">
                      <dt className="text-muted">{k}</dt>
                      <dd className="truncate text-right font-medium">{v}</dd>
                    </div>
                  ))}
                </dl>
                {mode === "simulated" && (
                  <p className="text-xs text-muted">
                    Simulated messages are labelled everywhere and can be deleted later in Settings.
                  </p>
                )}
                <ErrorText error={submit.error} />
              </>
            )}

            <div className="flex justify-between gap-3 pt-1">
              <Button type="button" variant="ghost" onClick={() => setStep(step - 1)} disabled={step === 0}>
                Back
              </Button>
              <Button type="submit" variant="primary" disabled={submit.isPending}>
                {step < 3 ? "Continue" : submit.isPending ? "Creating…" : "Finish setup"}
              </Button>
            </div>
          </form>
        </Card>
        {step === 0 && (
          <Card className="mt-4 p-5 sm:p-6">
            {restoring ? (
              token.trim() ? (
                <RestoreFlow setupToken={token.trim()} onCancel={() => setRestoring(false)} />
              ) : (
                <p className="text-sm">Enter the setup token above first; restoring needs it too.</p>
              )
            ) : (
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-sm text-muted">Moving from another MeshHome, or reinstalling?</p>
                <Button type="button" onClick={() => setRestoring(true)}>
                  <ArchiveRestore className="size-4" aria-hidden /> Restore a backup instead
                </Button>
              </div>
            )}
          </Card>
        )}
      </div>
    </div>
  );
}
