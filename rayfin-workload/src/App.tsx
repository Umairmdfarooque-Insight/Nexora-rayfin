// Local preview composition root. The real Starter-Kit project has its own
// `Workload/app/App.tsx` with host-provided routing, auth and item storage
// (README.md); this file exists so the pages and the item editor can be
// exercised without that toolkit in place, and to document how the pieces
// wire together once they're dropped into it. It is not built or served by
// this package's own scripts (there's no bundler configured here -- see
// README.md "What's not included").
import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { useState } from "react";

import type { ItemStorage } from "./items/NexoraRunItem/NexoraRunItemEditor.tsx";
import { NexoraRunItemEditor } from "./items/NexoraRunItem/NexoraRunItemEditor.tsx";
import type { NexoraRunItemDefinition } from "./items/NexoraRunItem/NexoraRunItemDefinition.ts";
import { NewRunPage } from "./pages/NewRunPage.tsx";
import { RunsListPage } from "./pages/RunsListPage.tsx";

type Route = { name: "runs" } | { name: "new" } | { name: "run"; id: string };

/** An in-memory stand-in for the Starter-Kit's `getWorkloadItem`/
 * `saveWorkloadItem`, scoped to one item instance, for local preview only. */
function makeItemStorage(initial: NexoraRunItemDefinition): ItemStorage {
  let stored = initial;
  return {
    getWorkloadItem: () => Promise.resolve(stored),
    saveWorkloadItem: (definition) => {
      stored = definition;
      return Promise.resolve();
    },
  };
}

export function App() {
  const [route, setRoute] = useState<Route>({ name: "runs" });
  const navigate = (path: string) => {
    if (path === "/runs/new") return setRoute({ name: "new" });
    if (path === "/runs") return setRoute({ name: "runs" });
    const match = /^\/runs\/([^/]+)/.exec(path);
    if (match) return setRoute({ name: "run", id: match[1] });
  };

  return (
    <FluentProvider theme={webLightTheme}>
      {route.name === "runs" && <RunsListPage navigate={navigate} />}
      {route.name === "new" && <NewRunPage navigate={navigate} />}
      {route.name === "run" && (
        <NexoraRunItemEditor itemStorage={makeItemStorage({ runId: route.id })} navigate={navigate} />
      )}
    </FluentProvider>
  );
}
