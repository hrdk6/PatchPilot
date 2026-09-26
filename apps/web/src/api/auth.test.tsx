import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ErrorBanner, DownloadLink } from "../components/common";
import { getApiKey, setApiKey } from "./auth";
import { api, PatchPilotApiError } from "./client";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function sentHeaders(fetchMock: ReturnType<typeof vi.fn>): Record<string, string> {
  const init = fetchMock.mock.calls[0]?.[1] as RequestInit | undefined;
  return (init?.headers ?? {}) as Record<string, string>;
}

describe("API key handling", () => {
  beforeEach(() => setApiKey(null));
  afterEach(() => setApiKey(null));

  it("sends no Authorization header when no key is saved", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, []));
    vi.stubGlobal("fetch", fetchMock);
    await api.models();
    expect(sentHeaders(fetchMock).Authorization).toBeUndefined();
    vi.unstubAllGlobals();
  });

  it("sends the saved key as a bearer token", async () => {
    setApiKey("secret-key");
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, []));
    vi.stubGlobal("fetch", fetchMock);
    await api.models();
    expect(sentHeaders(fetchMock).Authorization).toBe("Bearer secret-key");
    expect(sentHeaders(fetchMock)["Content-Type"]).toBe("application/json");
    vi.unstubAllGlobals();
  });

  it("surfaces a 401 with its status and code", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(401, { code: "unauthenticated", message: "a valid API key is required" }),
      ),
    );
    await expect(api.models()).rejects.toMatchObject({ status: 401, code: "unauthenticated" });
    vi.unstubAllGlobals();
  });

  it("asks for a key when a request was refused for the lack of one", async () => {
    const user = userEvent.setup();
    const reload = vi.fn();
    vi.stubGlobal("location", { ...window.location, reload });
    render(
      <ErrorBanner
        error={
          new PatchPilotApiError(401, {
            code: "unauthenticated",
            message: "a valid API key is required",
          })
        }
      />,
    );
    await user.type(screen.getByLabelText("API key"), "  typed-key  ");
    await user.click(screen.getByRole("button", { name: "Save key" }));
    expect(getApiKey()).toBe("typed-key");
    expect(reload).toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("does not offer a key form for other errors", () => {
    render(<ErrorBanner error={new PatchPilotApiError(500, { message: "boom" })} />);
    expect(screen.queryByLabelText("API key")).not.toBeInTheDocument();
  });

  it("downloads through an authenticated fetch when a key is saved", async () => {
    setApiKey("secret-key");
    const user = userEvent.setup();
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("diff --git", {
        status: 200,
        headers: { "Content-Disposition": 'attachment; filename="run_1.diff"' },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const createObjectURL = vi.fn().mockReturnValue("blob:1");
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
    // jsdom cannot navigate; record what the browser would have saved instead.
    const saved: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      saved.push(this.download);
    });

    render(
      <DownloadLink href="/api/v1/runs/run_1/patch.diff" filename="fallback.diff">
        Download diff
      </DownloadLink>,
    );
    await user.click(screen.getByRole("link", { name: "Download diff" }));

    expect(fetchMock).toHaveBeenCalledWith("/api/v1/runs/run_1/patch.diff", {
      headers: { Authorization: "Bearer secret-key" },
    });
    expect(createObjectURL).toHaveBeenCalled();
    expect(saved).toEqual(["run_1.diff"]);
    vi.unstubAllGlobals();
  });
});
