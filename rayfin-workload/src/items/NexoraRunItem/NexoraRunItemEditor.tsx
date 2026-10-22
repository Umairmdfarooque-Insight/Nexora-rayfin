// The "Nexora Run" item's editor: the run detail page (T-104 FR-1) and, next
// to it, the Contract review tab (T-205). This is the 4-file pattern's
// "Editor" component. `itemStorage` stands in for the Starter-Kit's own
// `getWorkloadItem`/`saveWorkloadItem` helpers (see README.md) until this
// project is dropped into the real toolkit project; the tab content itself
// doesn't depend on that detail.
import { Spinner, Tab, TabList } from "@fluentui/react-components";
import { useEffect, useState } from "react";

import { AuditPage } from "../../pages/AuditPage.tsx";
import { ContractPage } from "../../pages/ContractPage.tsx";
import { InventoryPage } from "../../pages/InventoryPage.tsx";
import { ModelPage } from "../../pages/ModelPage.tsx";
import { PlanPage } from "../../pages/PlanPage.tsx";
import { ReportPage } from "../../pages/ReportPage.tsx";
import { RunDetailPage } from "../../pages/RunDetailPage.tsx";
import { TracePage } from "../../pages/TracePage.tsx";
import { EMPTY_DEFINITION, NexoraRunItemDefinition } from "./NexoraRunItemDefinition.ts";
import { NexoraRunItemEditorEmpty } from "./NexoraRunItemEditorEmpty.tsx";
import { NexoraRunItemEditorRibbon } from "./NexoraRunItemEditorRibbon.tsx";

export interface ItemStorage {
  getWorkloadItem(): Promise<NexoraRunItemDefinition>;
  saveWorkloadItem(definition: NexoraRunItemDefinition): Promise<void>;
}

export function NexoraRunItemEditor({
  itemStorage,
  navigate,
}: {
  itemStorage: ItemStorage;
  navigate: (path: string) => void;
}) {
  const [definition, setDefinition] = useState<NexoraRunItemDefinition | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [tab, setTab] = useState<"overview" | "contract" | "inventory" | "plan" | "model" | "report" | "audit">("overview");
  const [traceVisualId, setTraceVisualId] = useState<string | null>(null);

  function selectTab(value: typeof tab) {
    setTraceVisualId(null); // T-703: leaving the Report tab (or re-entering it) closes any open trace
    setTab(value);
  }

  useEffect(() => {
    let cancelled = false;
    void itemStorage.getWorkloadItem().then((loaded) => {
      if (!cancelled) setDefinition(loaded ?? EMPTY_DEFINITION);
    });
    return () => {
      cancelled = true;
    };
  }, [itemStorage]);

  async function onCreated(runId: string) {
    await itemStorage.saveWorkloadItem({ runId });
    setDefinition({ runId });
  }

  if (definition === null) {
    return <Spinner label="Loading…" />;
  }

  if (!definition.runId) {
    return <NexoraRunItemEditorEmpty onCreated={(id) => void onCreated(id)} />;
  }

  return (
    <>
      <NexoraRunItemEditorRibbon onRefresh={() => setRefreshKey((n) => n + 1)} />
      <TabList
        selectedValue={tab}
        onTabSelect={(_, data) => selectTab(data.value as typeof tab)}
      >
        <Tab value="overview">Overview</Tab>
        <Tab value="contract">Contract</Tab>
        <Tab value="inventory">Inventory</Tab>
        <Tab value="plan">Plan</Tab>
        <Tab value="model">Model</Tab>
        <Tab value="report">Report</Tab>
        <Tab value="audit">Audit</Tab>
      </TabList>
      {tab === "overview" && (
        <RunDetailPage
          key={refreshKey}
          runId={definition.runId}
          navigate={navigate}
          onOpenContract={() => selectTab("contract")}
        />
      )}
      {tab === "contract" && <ContractPage key={refreshKey} runId={definition.runId} />}
      {tab === "inventory" && <InventoryPage key={refreshKey} runId={definition.runId} />}
      {tab === "plan" && <PlanPage key={refreshKey} runId={definition.runId} />}
      {tab === "model" && <ModelPage key={refreshKey} runId={definition.runId} />}
      {tab === "report" &&
        (traceVisualId ? (
          <TracePage
            key={`${refreshKey}-${traceVisualId}`}
            runId={definition.runId}
            visualId={traceVisualId}
            onBack={() => setTraceVisualId(null)}
            onOpenAudit={() => selectTab("audit")}
          />
        ) : (
          <ReportPage key={refreshKey} runId={definition.runId} onTrace={(id) => setTraceVisualId(id)} />
        ))}
      {tab === "audit" && <AuditPage key={refreshKey} runId={definition.runId} />}
    </>
  );
}
