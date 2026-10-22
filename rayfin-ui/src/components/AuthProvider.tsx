"use client";

import {
  InteractionStatus,
  type PublicClientApplication,
} from "@azure/msal-browser";
import { MsalProvider, useMsal } from "@azure/msal-react";
import {
  Button,
  makeStyles,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
  Spinner,
} from "@fluentui/react-components";
import { useEffect, useState, type ReactNode } from "react";

import {
  acquireApiToken,
  apiScope,
  getMsalInstance,
  missingAuthSettings,
} from "@/lib/auth";
import { setTokenProvider } from "@/lib/api";

const useStyles = makeStyles({
  center: { padding: "48px 24px", maxWidth: "720px", margin: "0 auto" },
});

type Status =
  | { kind: "loading" }
  | { kind: "unconfigured" }
  | { kind: "error"; message: string }
  | { kind: "ready"; msal: PublicClientApplication };

/** Signs the user in before any page renders. There is no way to skip it. */
export function AuthProvider({ children }: { children: ReactNode }) {
  const styles = useStyles();
  const [status, setStatus] = useState<Status>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const msal = getMsalInstance();
    if (!msal) {
      setStatus({ kind: "unconfigured" });
      return;
    }
    setStatus({ kind: "loading" });
    (async () => {
      await msal.initialize();
      const result = await msal.handleRedirectPromise();
      const account = result?.account ?? msal.getAllAccounts()[0];
      if (account) {
        msal.setActiveAccount(account);
      }
      setStatus({ kind: "ready", msal });
    })().catch((error: unknown) => {
      const message = error instanceof Error ? error.message : "Sign-in failed.";
      setStatus({ kind: "error", message });
    });
  }, [attempt]);

  if (status.kind === "loading") {
    return (
      <div className={styles.center}>
        <Spinner label="Signing in…" />
      </div>
    );
  }

  if (status.kind === "unconfigured") {
    return (
      <div className={styles.center}>
        <MessageBar intent="error" role="alert">
          <MessageBarBody>
            <MessageBarTitle>Sign-in is not configured.</MessageBarTitle>
            Set {missingAuthSettings.join(", ")} in rayfin-ui/.env.local, then restart
            the UI.
          </MessageBarBody>
        </MessageBar>
      </div>
    );
  }

  if (status.kind === "error") {
    return (
      <div className={styles.center}>
        <MessageBar intent="error" role="alert">
          <MessageBarBody>
            <MessageBarTitle>Sign-in failed.</MessageBarTitle>
            {status.message}
          </MessageBarBody>
          <MessageBarActions>
            <Button onClick={() => setAttempt((n) => n + 1)}>Try again</Button>
          </MessageBarActions>
        </MessageBar>
      </div>
    );
  }

  return (
    <MsalProvider instance={status.msal}>
      <AuthGate>{children}</AuthGate>
    </MsalProvider>
  );
}

function AuthGate({ children }: { children: ReactNode }) {
  const styles = useStyles();
  const { instance, accounts, inProgress } = useMsal();
  const [tokenReady, setTokenReady] = useState(false);
  const signedIn = accounts.length > 0;

  useEffect(() => {
    if (!signedIn && inProgress === InteractionStatus.None) {
      void instance.loginRedirect({ scopes: [apiScope] });
    }
  }, [signedIn, inProgress, instance]);

  useEffect(() => {
    if (signedIn) {
      setTokenProvider((forceRefresh) => acquireApiToken(instance, forceRefresh));
      setTokenReady(true);
    }
  }, [signedIn, instance]);

  if (!signedIn || !tokenReady) {
    return (
      <div className={styles.center}>
        <Spinner label="Signing in…" />
      </div>
    );
  }
  return <>{children}</>;
}
