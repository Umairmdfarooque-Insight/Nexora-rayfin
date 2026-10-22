"use client";

import {
  Button,
  MessageBar,
  MessageBarActions,
  MessageBarBody,
  MessageBarTitle,
} from "@fluentui/react-components";
import type { ReactNode } from "react";

import type { ApiError } from "@/lib/api";

interface Props {
  title: string;
  error: ApiError | Error;
  onRetry?: () => void;
  disabled?: boolean;
  children?: ReactNode;
}

export function ErrorBanner({ title, error, onRetry, disabled, children }: Props) {
  return (
    <MessageBar intent="error" role="alert">
      <MessageBarBody>
        <MessageBarTitle>{title}</MessageBarTitle>
        {error.message}
        {children}
      </MessageBarBody>
      {onRetry && (
        <MessageBarActions>
          <Button onClick={onRetry} disabled={disabled}>
            Retry
          </Button>
        </MessageBarActions>
      )}
    </MessageBar>
  );
}
