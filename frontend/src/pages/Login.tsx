import { useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import { api, type Me } from "../lib/api";
import { Button, Card, ErrorText, Field, Input } from "../components/ui";

export function Login() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const login = useMutation({
    mutationFn: () => api<Me>("/api/auth/login", { json: { username, password } }),
    onSuccess: (me) => {
      qc.setQueryData(["me"], me);
      qc.invalidateQueries({ queryKey: ["me"] });
      navigate("/", { replace: true });
    },
  });

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (username && password) login.mutate();
  };

  return (
    <div className="flex min-h-dvh items-center justify-center bg-bg px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center gap-3 text-center">
          <img src="/favicon.svg" alt="" className="size-12" />
          <h1 className="text-xl font-semibold">
            Mesh<span className="text-accent">Home</span>
          </h1>
        </div>
        <Card className="p-5 sm:p-6">
          <form onSubmit={submit} className="space-y-4">
            <Field label="Username" htmlFor="username">
              <Input
                id="username"
                autoFocus
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </Field>
            <Field label="Password" htmlFor="password">
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>
            <ErrorText error={login.error} />
            <Button type="submit" variant="primary" className="w-full" disabled={login.isPending || !username || !password}>
              {login.isPending ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        </Card>
      </div>
    </div>
  );
}
