import { createConnection } from "@playwright/mcp";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import fs from "node:fs";
import readline from "node:readline";

function normalizeTextParts(content) {
  if (!Array.isArray(content))
    return [];
  return content
    .filter((item) => item && item.type === "text")
    .map((item) => String(item.text ?? ""));
}

function extractStructuredContent(result) {
  if (result && typeof result === "object" && result.structuredContent && typeof result.structuredContent === "object")
    return result.structuredContent;
  return {};
}

function extractResultBlock(text) {
  const source = String(text || "");
  const match = source.match(/### Result\s*([\s\S]*?)(?:\n### |\s*$)/);
  return match ? match[1].trim() : "";
}

function parseTabsFromText(result) {
  const text = normalizeTextParts(result?.content).join("\n");
  const block = extractResultBlock(text);
  if (!block)
    return [];
  const lines = block.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const tabs = [];
  for (const line of lines) {
    const match = line.match(/^-\s+(\d+):\s+(\(current\)\s+)?\[(.*?)\]\((.*?)\)$/);
    if (!match)
      continue;
    const index = Number(match[1]);
    tabs.push({
      index,
      tab_id: String(index),
      title: String(match[3] || ""),
      url: String(match[4] || ""),
      active: Boolean(match[2]),
    });
  }
  return tabs;
}

function extractSnapshotMarkdown(result) {
  const textParts = normalizeTextParts(result?.content);
  if (textParts.length)
    return textParts.join("\n\n");
  const structured = extractStructuredContent(result);
  if (typeof structured.snapshot === "string")
    return structured.snapshot;
  if (typeof structured.markdown === "string")
    return structured.markdown;
  return "";
}

function extractEvaluateValue(result) {
  const structured = extractStructuredContent(result);
  if (Object.prototype.hasOwnProperty.call(structured, "result"))
    return structured.result;
  if (Object.prototype.hasOwnProperty.call(structured, "value"))
    return structured.value;
  const textParts = normalizeTextParts(result?.content);
  if (!textParts.length)
    return null;
  const merged = textParts.join("\n");
  const resultBlock = extractResultBlock(merged);
  if (resultBlock) {
    try {
      return JSON.parse(resultBlock);
    } catch {
    }
    return resultBlock;
  }
  try {
    return JSON.parse(merged);
  } catch {
    return merged;
  }
}

function simplifyToolResult(result) {
  return {
    isError: Boolean(result?.isError),
    structuredContent: extractStructuredContent(result),
    textParts: normalizeTextParts(result?.content),
  };
}

class OfficialBridgeRuntime {
  constructor(config) {
    this.config = config;
    this.server = null;
    this.client = null;
    this.state = { tabs: [], active_tab_id: "", url: "", title: "", alive: true };
  }

  async start() {
    this.server = await createConnection({
      browser: {
        userDataDir: this.config.browser.userDataDir,
        launchOptions: {
          executablePath: this.config.browser.chromiumExecutable,
          chromiumSandbox: true,
          headless: Boolean(this.config.browser.headless),
          args: [
            `--profile-directory=${String(this.config.browser.profileName || "Profile 1")}`,
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
          ],
          ignoreDefaultArgs: ["--enable-automation"],
        },
      },
    });
    const extensionDirs = Array.isArray(this.config?.browser?.extensionDirs)
      ? this.config.browser.extensionDirs.map(item => String(item || "").trim()).filter(Boolean)
      : [];
    if (extensionDirs.length) {
      this.server.options.browser.launchOptions.args.push(`--load-extension=${extensionDirs.join(",")}`);
    }
    this.client = new Client({ name: "chromium-advanced-official-bridge", version: "0.1.0" });
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    await Promise.all([this.server.connect(serverTransport), this.client.connect(clientTransport)]);
  }

  async stop() {
    await this.client?.close().catch(() => {});
    await this.server?.close().catch(() => {});
    this.client = null;
    this.server = null;
  }

  async listTabsAndState() {
    const tabsResult = await this.client.callTool({ name: "browser_tabs", arguments: { action: "list" } });
    const simplified = simplifyToolResult(tabsResult);
    const structured = simplified.structuredContent || {};
    const tabs = Array.isArray(structured.tabs) ? structured.tabs.map((item, index) => ({
      index,
      tab_id: String(item.tabId ?? item.id ?? `${index}`),
      url: String(item.url ?? ""),
      title: String(item.title ?? ""),
      active: Boolean(item.active ?? false),
    })) : parseTabsFromText(tabsResult);
    const active = tabs.find((item) => item.active) ?? tabs[0] ?? null;
    this.state = {
      tabs,
      active_tab_id: active ? active.tab_id : "",
      url: active ? active.url : "",
      title: active ? active.title : "",
      alive: true,
    };
    return this.state;
  }

  async executeAction(action) {
    const name = String(action?.name || "");
    const args = action?.arguments && typeof action.arguments === "object" ? action.arguments : {};
    if (!name)
      throw new Error("missing action name");

    const toolResult = await this.client.callTool({ name, arguments: args });
    const state = await this.listTabsAndState().catch(() => this.state);
    const response = {
      ok: !toolResult?.isError,
      tool_name: name,
      tool_result: simplifyToolResult(toolResult),
      state,
    };

    if (name === "browser_snapshot") {
      response.snapshot = extractSnapshotMarkdown(toolResult);
      response.snapshot_structured = extractStructuredContent(toolResult);
    } else if (name === "browser_evaluate") {
      response.result = extractEvaluateValue(toolResult);
    } else if (name === "browser_take_screenshot") {
      const structured = extractStructuredContent(toolResult);
      response.path = String(structured.path ?? structured.filename ?? args.filename ?? "");
    }

    if (toolResult?.isError) {
      response.error = normalizeTextParts(toolResult?.content).join("\n").trim() || JSON.stringify(response.tool_result.structuredContent || {});
    }
    return response;
  }
}

function writeResponse(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

async function main() {
  const rawInput = process.argv[2] || "";
  if (!rawInput)
    throw new Error("missing bridge payload");
  const raw = fs.existsSync(rawInput) ? fs.readFileSync(rawInput, "utf8") : rawInput;
  const payload = JSON.parse(raw);
  const runtime = new OfficialBridgeRuntime(payload);
  await runtime.start();
  writeResponse({ ok: true, event: "ready" });

  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of rl) {
    const trimmed = String(line || "").trim();
    if (!trimmed)
      continue;
    let request;
    try {
      request = JSON.parse(trimmed);
    } catch (error) {
      writeResponse({ ok: false, error: String(error?.stack || error) });
      continue;
    }
    if (request?.command === "close") {
      await runtime.stop();
      writeResponse({ ok: true, closed: true });
      break;
    }
    try {
      const result = await runtime.executeAction(request?.action || {});
      writeResponse(result);
    } catch (error) {
      writeResponse({ ok: false, error: String(error?.stack || error) });
    }
  }
}

main().catch((error) => {
  writeResponse({
    ok: false,
    error: String(error?.stack || error),
  });
  process.exitCode = 1;
});
