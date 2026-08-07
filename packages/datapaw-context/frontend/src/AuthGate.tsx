import { Alert, Button, Card, Input, Spin, Typography } from "@/design";
import React, { FormEvent, useCallback, useEffect, useState } from "react";

import {
  AUTH_CHANGE_EVENT,
  clearAuthToken,
  getApiToken,
  getApiUrl,
  setAuthToken,
} from "@/services/config";

type GateState = "loading" | "open" | "locked" | "error";

async function authRequired(): Promise<boolean> {
  const response = await fetch(getApiUrl("/auth/status"));
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const payload = (await response.json()) as { required?: unknown };
  if (typeof payload.required !== "boolean") {
    throw new Error("Invalid authentication status response");
  }
  return payload.required;
}

async function tokenIsValid(token: string): Promise<boolean> {
  const response = await fetch(getApiUrl("/auth/check"), {
    headers: { Authorization: `Bearer ${token}` },
  });
  return response.ok;
}

export default function AuthGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<GateState>("loading");
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const bootstrap = useCallback(async () => {
    setState("loading");
    setError("");
    try {
      const required = await authRequired();
      if (!required) {
        setState("open");
        return;
      }
      const stored = getApiToken();
      if (stored && (await tokenIsValid(stored))) {
        setState("open");
        return;
      }
      if (stored) clearAuthToken();
      setState("locked");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unknown error");
      setState("error");
    }
  }, []);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  useEffect(() => {
    const onAuthChange = () => {
      if (!getApiToken()) setState("locked");
    };
    window.addEventListener(AUTH_CHANGE_EVENT, onAuthChange);
    return () => window.removeEventListener(AUTH_CHANGE_EVENT, onAuthChange);
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const candidate = token.trim();
    if (!candidate) return;
    setSubmitting(true);
    setError("");
    try {
      if (!(await tokenIsValid(candidate))) {
        setError("访问令牌无效 / Invalid access token");
        return;
      }
      setAuthToken(candidate);
      setToken("");
      setState("open");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Network error");
    } finally {
      setSubmitting(false);
    }
  };

  if (state === "open") return <>{children}</>;

  return (
    <main
      style={{
        minHeight: "100vh",
        display: "grid",
        placeItems: "center",
        padding: 24,
        background: "#f5f7fa",
      }}
    >
      {state === "loading" ? (
        <Spin size="large" />
      ) : (
        <Card style={{ width: "min(420px, 100%)" }}>
          <Typography.Title level={3}>DataPaw</Typography.Title>
          {state === "error" ? (
            <>
              <Alert
                type="error"
                showIcon
                message="无法检查认证状态 / Authentication check failed"
                description={error}
              />
              <Button type="primary" onClick={() => void bootstrap()} style={{ marginTop: 16 }}>
                重试 / Retry
              </Button>
            </>
          ) : (
            <form onSubmit={(event) => void submit(event)}>
              <Typography.Text style={{ display: "block", marginBottom: 12 }}>
                请输入服务端配置的访问令牌。令牌只保存在当前浏览器会话中。
              </Typography.Text>
              <Input.Password
                autoFocus
                autoComplete="current-password"
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder="Access token"
              />
              {error ? (
                <Alert type="error" showIcon message={error} style={{ marginTop: 12 }} />
              ) : null}
              <Button
                block
                htmlType="submit"
                type="primary"
                loading={submitting}
                disabled={!token.trim()}
                style={{ marginTop: 16 }}
              >
                登录 / Continue
              </Button>
            </form>
          )}
        </Card>
      )}
    </main>
  );
}
