"use client";

import { Body1, Button, makeStyles } from "@fluentui/react-components";
import { useState } from "react";

import { collapsed, COLLAPSE_AT } from "@/lib/review";

const useStyles = makeStyles({
  text: { whiteSpace: "pre-wrap", overflowWrap: "anywhere" },
});

/** Text that collapses past a length limit, with a Show more / Show less toggle. */
export function LongText({ text }: { text: string }) {
  const styles = useStyles();
  const [expanded, setExpanded] = useState(false);
  const shown = collapsed(text, expanded);
  return (
    <>
      <Body1 className={styles.text}>{shown.text}</Body1>
      {text.length > COLLAPSE_AT && (
        <div>
          <Button
            appearance="transparent"
            size="small"
            onClick={() => setExpanded(!expanded)}
            aria-expanded={expanded}
          >
            {expanded ? "Show less" : "Show more"}
          </Button>
        </div>
      )}
    </>
  );
}
