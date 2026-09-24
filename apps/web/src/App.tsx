import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import { api } from "./api/client";
import { useResource } from "./api/hooks";
import { AlertIcon, CheckIcon, PlusIcon, ShieldIcon } from "./components/icons";
import { BenchmarkDetailPage } from "./pages/BenchmarkDetailPage";
import { BenchmarksPage } from "./pages/BenchmarksPage";
import { NewRunPage } from "./pages/NewRunPage";
import { RunDetailPage } from "./pages/RunDetailPage";
import { RunsPage } from "./pages/RunsPage";

function SystemIndicator(): JSX.Element {
  const system = useResource(() => api.system(), [], { pollMs: 15000 });
  if (system.error) {
    return (
      <span className="label label-warn" title={system.error.remediation ?? undefined}>
        <AlertIcon /> API unreachable
      </span>
    );
  }
  if (!system.data) {
    return <span className="label label-neutral">connecting…</span>;
  }
  const { sandbox, worker_running, queued_jobs, version } = system.data;
  return (
    <>
      <span
        className={sandbox.isolated ? "label label-neutral" : "label label-warn"}
        title={sandbox.reason}
      >
        {sandbox.isolated ? <ShieldIcon /> : <AlertIcon />}
        sandbox {sandbox.backend}
        {sandbox.isolated ? "" : " · not isolated"}
      </span>
      <span className={worker_running ? "label label-neutral" : "label label-warn"}>
        {worker_running ? <CheckIcon /> : <AlertIcon />}
        worker {worker_running ? "up" : "down"}
        {queued_jobs > 0 ? ` · ${queued_jobs} queued` : ""}
      </span>
      <span className="faint mono">v{version}</span>
    </>
  );
}

export function App(): JSX.Element {
  return (
    <div className="app">
      <header className="topbar">
        <NavLink to="/runs" className="brand" aria-label="PatchPilot, all runs">
          <svg className="brand-mark" viewBox="0 0 20 20" aria-hidden="true">
            <rect x="1" y="1" width="18" height="18" rx="3" />
            <path d="M6 7h5M8.5 4.5v5M6 14h8" />
          </svg>
          PatchPilot
        </NavLink>
        <nav className="nav" aria-label="Main">
          <NavLink to="/runs">Runs</NavLink>
          <NavLink to="/benchmarks">Benchmarks</NavLink>
        </nav>
        <NavLink to="/new" className="button primary nav-new">
          <PlusIcon /> New run
        </NavLink>
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
                <div>
                  <h3>Page not found</h3>
                  <p>
                    <NavLink to="/runs">Back to runs</NavLink>
                  </p>
                </div>
              </div>
            }
          />
        </Routes>
      </main>
    </div>
  );
}
