// Microsoft Entra ID sign-in for Rayfin (MSAL, authorization code flow with PKCE).

import {
  InteractionRequiredAuthError,
  PublicClientApplication,
  type IPublicClientApplication,
} from "@azure/msal-browser";

// NEXT_PUBLIC_* values are inlined at build time, so each must be written out in full.
const tenantId = process.env.NEXT_PUBLIC_ENTRA_TENANT_ID ?? "";
const clientId = process.env.NEXT_PUBLIC_ENTRA_CLIENT_ID ?? "";
export const apiScope = process.env.NEXT_PUBLIC_API_SCOPE ?? "";

export const missingAuthSettings: string[] = [
  ["NEXT_PUBLIC_ENTRA_TENANT_ID", tenantId],
  ["NEXT_PUBLIC_ENTRA_CLIENT_ID", clientId],
  ["NEXT_PUBLIC_API_SCOPE", apiScope],
]
  .filter(([, value]) => !value.trim())
  .map(([name]) => name);

let instance: PublicClientApplication | null = null;

/** The one MSAL instance, or null when settings are missing or on the server. */
export function getMsalInstance(): PublicClientApplication | null {
  if (missingAuthSettings.length > 0 || typeof window === "undefined") {
    return null;
  }
  instance ??= new PublicClientApplication({
    auth: {
      clientId,
      authority: `https://login.microsoftonline.com/${tenantId}`,
      redirectUri: window.location.origin,
      postLogoutRedirectUri: window.location.origin,
    },
    cache: { cacheLocation: "sessionStorage" },
  });
  return instance;
}

/** An access token for the Nexora API. Refreshed silently when it expires. */
export async function acquireApiToken(
  msal: IPublicClientApplication,
  forceRefresh = false,
): Promise<string> {
  const account = msal.getActiveAccount() ?? msal.getAllAccounts()[0];
  if (!account) {
    throw new Error("Not signed in");
  }
  try {
    const result = await msal.acquireTokenSilent({
      scopes: [apiScope],
      account,
      forceRefresh,
    });
    return result.accessToken;
  } catch (error) {
    if (error instanceof InteractionRequiredAuthError) {
      await msal.acquireTokenRedirect({ scopes: [apiScope], account });
    }
    throw error;
  }
}
