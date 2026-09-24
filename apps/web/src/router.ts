/**
 * React Router v7 behaviour, opted into now so the upgrade is a version bump
 * rather than a migration -- and so the tests do not print upgrade warnings.
 * Shared by the app and the tests so both run under the same router semantics.
 */
export const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true } as const;
