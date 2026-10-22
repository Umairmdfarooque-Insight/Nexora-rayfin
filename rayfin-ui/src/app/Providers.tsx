"use client";

import {
  createDOMRenderer,
  FluentProvider,
  RendererProvider,
  renderToStyleElements,
  SSRProvider,
  webLightTheme,
} from "@fluentui/react-components";
import { useServerInsertedHTML } from "next/navigation";
import { useRef, useState, type ReactNode } from "react";

import { AuthProvider } from "@/components/AuthProvider";

// Fluent UI v9 style handling for the Next.js app router: collect the styles
// rendered on the server and insert them into the HTML head.
export function Providers({ children }: { children: ReactNode }) {
  const [renderer] = useState(() => createDOMRenderer());
  const inserted = useRef(false);

  useServerInsertedHTML(() => {
    if (inserted.current) {
      return null;
    }
    inserted.current = true;
    return <>{renderToStyleElements(renderer)}</>;
  });

  return (
    <RendererProvider renderer={renderer}>
      <SSRProvider>
        <FluentProvider theme={webLightTheme}>
          <AuthProvider>{children}</AuthProvider>
        </FluentProvider>
      </SSRProvider>
    </RendererProvider>
  );
}
