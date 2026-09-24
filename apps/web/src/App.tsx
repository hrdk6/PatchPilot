import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import { api } from "./api/client";
import { useResource } from "./api/hooks";
import { BenchmarkDetailPage } from "./pages/BenchmarkDetailPage";
import { BenchmarksPage } from "./pages/BenchmarksPage";
import { NewRunPage } from "./pages/NewRunPage";
import { RunDetailPage } from "./pages/RunDetailPage";
import { RunsPage } from "./pages/RunsPage";

function SystemIndicator(): JSX.Element {
  const system = useResource(() => api.system(), [], { pollMs: 15000 });
  if (system.error) {
    return (
      <span className="badge badge-danger" title={system.error.remediation ?? undefined}>
        API unreachable
      </span>
    );
  }
  if (!system.data) {
    return <span className="badge">connecting…</span>;
  }
  const { sandbox, worker_running, queued_jobs, version } = system.data;
  return (
    <>
      <span
        className={sandbox.isolated ? "badge badge-ok" : "badge badge-warn"}
        title={sandbox.reason}
      >
        sandbox: {sandbox.backend}
        {sandbox.isolated ? "" : " (not isolated)"}
      </span>
      <span className={worker_running ? "badge badge-ok" : "badge badge-warn"}>
        worker {worker_running ? "up" : "down"}
        {queued_jobs > 0 ? ` · ${queued_jobs} queued` : ""}
      </span>
      <span className="faint">v{version}</span>
    </>
  );
}

export function App(): JSX.Element {
  return (
    <div className="app">
      <header className="topbar">
        <NavLink to="/runs" className="brand">
          <span className="brand-mark" aria-hidden="true">
            P
          </span>
          PatchPilot
        </NavLink>
        <nav className="nav" aria-label="Main">
          <NavLink to="/new">New run</NavLink>
          <NavLink to="/runs">Runs</NavLink>
          <NavLink to="/benchmarks">Benchmarks</NavLink>
        </nav>
        <div className="topbar-right">
          <SystemIndicator />
        </div>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Navigate to="/runs" replace />} />
          <Route path="/new" element={<NewRunPage />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/runs/:runId" element={<RunDetailPage />} />
          <Route path="/benchmarks" element={<BenchmarksPage />} />
          <Route path="/benchmarks/:benchmarkId" element={<BenchmarkDetailPage />} />
          <Route
            path="*"
            element={
              <div className="empty">
                <h3>Page not found</h3>
                <p>
                  <NavLink to="/runs">Back to runs</NavLink>
                </p>
              </div>
            }
          />
        </Routes>
      </main>
    </div>
  );
}
