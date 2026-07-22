import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

describe("Vite API proxy config", () => {
  const configPath = path.resolve(__dirname, "../../vite.config.ts");
  const config = fs.readFileSync(configPath, "utf8");

  it("proxies channel runtime endpoints", () => {
    expect(config).toContain('"/channels"');
  });

  it("proxies settings endpoints", () => {
    expect(config).toContain('"/settings/llm"');
    expect(config).toContain('"/settings/data-sources"');
  });

  it("isolates VeriQuant on its dedicated loopback API", () => {
    expect(config).toContain('env.VITE_VERIQUANT_API_URL || "http://127.0.0.1:8900"');
    for (const path of [
      "/api/veriquant",
      "/healthz",
      "/veriquant/status",
      "/veriquant/workbench/runs",
      "/veriquant/operator",
      "/veriquant/jobs",
    ]) {
      expect(config).toContain(`"${path}"`);
    }
    expect(config).toContain("VERIQUANT_API_PATHS.map((p) => [p, veriquantApiProxy])");
    const legacyProxyList = config.match(/PROXY_PATHS\s*=\s*(\[[\s\S]*?\]);/)?.[1] || "";
    expect(legacyProxyList).not.toContain('"/veriquant"');
  });
});
