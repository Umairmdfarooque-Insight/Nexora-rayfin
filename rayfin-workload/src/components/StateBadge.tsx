import { Badge } from "@fluentui/react-components";

type Color = "brand" | "danger" | "important" | "informative" | "success" | "warning";

// Colour is presentation only: it groups states by the suffix of their name.
function colorFor(state: string): Color {
  if (state === "COMPLETED") return "success";
  if (state.endsWith("FAILED") || state === "ROLLED_BACK") return "danger";
  if (state.startsWith("NEEDS")) return "warning";
  if (state.endsWith("APPROVED")) return "success";
  if (state === "CREATED") return "informative";
  return "brand";
}

export function StateBadge({ state }: { state: string }) {
  return (
    <Badge appearance="filled" color={colorFor(state)} size="large">
      {state}
    </Badge>
  );
}
