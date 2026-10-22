// First-time experience for a "Nexora Run" item with no run_id yet (the
// 4-file pattern's "Empty" component). In practice this should be rare: T-104
// FR-5 creates the run (POST /runs) at the moment the item is created, before
// the editor ever opens. It exists so an item that somehow lost its payload
// still has something other than a blank screen.
import { Body1, Button, Spinner, Title2, makeStyles } from "@fluentui/react-components";
import { useState } from "react";

import { api } from "../../lib/api.ts";

const useStyles = makeStyles({
  page: { display: "flex", flexDirection: "column", rowGap: "12px", padding: "24px" },
});

export function NexoraRunItemEditorEmpty({ onCreated }: { onCreated: (runId: string) => void }) {
  const styles = useStyles();
  const [creating, setCreating] = useState(false);

  async function createBlankRun() {
    setCreating(true);
    try {
      const run = await api.createRun({ prompt: "Describe the requirement", desired_outputs: [], constraints: [] });
      onCreated(run.run_id);
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className={styles.page}>
      <Title2 as="h1">New Nexora Run</Title2>
      <Body1>This item isn't linked to a run yet.</Body1>
      <Button appearance="primary" disabled={creating} onClick={() => void createBlankRun()}>
        {creating ? <Spinner size="tiny" /> : "Start a run"}
      </Button>
    </div>
  );
}
