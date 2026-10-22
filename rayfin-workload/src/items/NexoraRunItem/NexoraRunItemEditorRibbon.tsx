// The item editor's toolbar (the 4-file pattern's "Ribbon" component). Kept
// deliberately small: the run page itself has its own Refresh button, and
// every other action (approve, regenerate, deploy, ...) arrives with its own
// UI task. This just gives the item a Fabric-standard ribbon slot to extend.
import { Toolbar, ToolbarButton } from "@fluentui/react-components";
import { ArrowClockwiseRegular } from "@fluentui/react-icons";

export function NexoraRunItemEditorRibbon({ onRefresh }: { onRefresh: () => void }) {
  return (
    <Toolbar aria-label="Nexora Run actions">
      <ToolbarButton icon={<ArrowClockwiseRegular />} onClick={onRefresh}>
        Refresh
      </ToolbarButton>
    </Toolbar>
  );
}
