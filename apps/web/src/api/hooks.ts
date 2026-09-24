/**
 * Data-fetching hooks with explicit loading, empty and error states.
 *
 * `useResource` returns a discriminated state rather than `data | undefined`, so
 * a component cannot accidentally render an empty table while a request is still
 * in flight -- which is how dashboards end up claiming "no results" when they
 * simply have not loaded yet.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { PatchPilotApiError } from "./client";

export interface ResourceState<T> {
  data: T | null;
  error: PatchPilotApiError | null;
  loading: boolean;
  /** True only for the first load, so refreshes do not blank the screen. */
  initialLoading: boolean;
  refresh: () => void;
}

export function useResource<T>(
  fetcher: () => Promise<T>,
  deps: unknown[],
  options: { pollMs?: number; enabled?: boolean } = {},
): ResourceState<T> {
  const { pollMs, enabled = true } = options;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<PatchPilotApiError | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [initialLoading, setInitialLoading] = useState(enabled);
  const mounted = useRef(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    if (!enabled) return;
    setLoading(true);
    try {
      const result = await fetcherRef.current();
      if (!mounted.current) return;
      setData(result);
      setError(null);
    } catch (cause) {
      if (!mounted.current) return;
      setError(
        cause instanceof PatchPilotApiError
          ? cause
          : new PatchPilotApiError(0, { message: String(cause) }),
      );
    } finally {
      if (mounted.current) {
        setLoading(false);
        setInitialLoading(false);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...deps]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!pollMs || !enabled) return;
    const timer = window.setInterval(() => void load(), pollMs);
    return () => window.clearInterval(timer);
  }, [pollMs, enabled, load]);

  return { data, error, loading, initialLoading, refresh: () => void load() };
}

export function useSubmit<TInput, TOutput>(
  action: (input: TInput) => Promise<TOutput>,
): {
  submit: (input: TInput) => Promise<TOutput | null>;
  submitting: boolean;
  error: PatchPilotApiError | null;
  reset: () => void;
} {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<PatchPilotApiError | null>(null);

  const submit = useCallback(
    async (input: TInput) => {
      setSubmitting(true);
      setError(null);
      try {
        return await action(input);
      } catch (cause) {
        setError(
          cause instanceof PatchPilotApiError
            ? cause
            : new PatchPilotApiError(0, { message: String(cause) }),
        );
        return null;
      } finally {
        setSubmitting(false);
      }
    },
    [action],
  );

  return { submit, submitting, error, reset: () => setError(null) };
}
