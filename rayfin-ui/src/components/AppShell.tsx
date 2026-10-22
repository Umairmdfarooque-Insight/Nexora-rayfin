"use client";

import { useMsal } from "@azure/msal-react";
import { Button, makeStyles, tokens } from "@fluentui/react-components";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

const useStyles = makeStyles({
  root: {
    minHeight: "100vh",
    backgroundColor: tokens.colorNeutralBackground2,
    color: tokens.colorNeutralForeground1,
    fontFamily: tokens.fontFamilyBase,
  },
  header: {
    display: "flex",
    alignItems: "center",
    columnGap: "32px",
    padding: "0 24px",
    height: "48px",
    backgroundColor: tokens.colorBrandBackground,
    color: tokens.colorNeutralForegroundOnBrand,
  },
  brand: {
    fontSize: tokens.fontSizeBase500,
    fontWeight: tokens.fontWeightSemibold,
    color: "inherit",
    textDecorationLine: "none",
  },
  nav: { display: "flex", columnGap: "8px" },
  user: {
    marginLeft: "auto",
    display: "flex",
    alignItems: "center",
    columnGap: "12px",
    fontSize: tokens.fontSizeBase300,
  },
  signOut: {
    color: "inherit",
    ":hover": { backgroundColor: tokens.colorBrandBackgroundHover, color: "inherit" },
  },
  link: {
    color: "inherit",
    textDecorationLine: "none",
    padding: "6px 12px",
    borderRadius: tokens.borderRadiusMedium,
    ":hover": { backgroundColor: tokens.colorBrandBackgroundHover },
    ":focus-visible": { outline: "2px solid white", outlineOffset: "2px" },
  },
  active: {
    backgroundColor: tokens.colorBrandBackgroundPressed,
    fontWeight: tokens.fontWeightSemibold,
  },
  main: { padding: "16px 24px", maxWidth: "1200px", margin: "0 auto" },
});

const NAV = [
  { href: "/runs", label: "Runs" },
  { href: "/runs/new", label: "New run" },
];

export function AppShell({ children }: { children: ReactNode }) {
  const styles = useStyles();
  const pathname = usePathname();
  const { instance, accounts } = useMsal();
  const account = accounts[0];

  return (
    <div className={styles.root}>
      <header className={styles.header}>
        <Link href="/runs" className={styles.brand}>
          Nexora
        </Link>
        <nav className={styles.nav} aria-label="Main">
          {NAV.map(({ href, label }) => (
            <Link
              key={href}
              href={href}
              className={`${styles.link} ${pathname === href ? styles.active : ""}`}
              aria-current={pathname === href ? "page" : undefined}
            >
              {label}
            </Link>
          ))}
        </nav>
        {account && (
          <div className={styles.user}>
            <span title={account.username}>{account.name ?? account.username}</span>
            <Button
              size="small"
              appearance="subtle"
              className={styles.signOut}
              onClick={() => void instance.logoutRedirect()}
            >
              Sign out
            </Button>
          </div>
        )}
      </header>
      <main className={styles.main}>{children}</main>
    </div>
  );
}
