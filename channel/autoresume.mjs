#!/usr/bin/env node
/**
 * One-way channel MCP server for claude-autoresume.
 *
 * Listens on a local HTTP port. The daemon POSTs a resume prompt
 * when a rate limit expires, and this server forwards it into the
 * running Claude Code session as a channel notification.
 */

import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";

const PORT = parseInt(process.env.AUTORESUME_PORT || "18963", 10);
const TOKEN_PATH =
  process.env.AUTORESUME_TOKEN_FILE ||
  join(homedir(), ".local", "share", "claude-autoresume", "auth-token");

function loadAuthToken() {
  try {
    return readFileSync(TOKEN_PATH, "utf-8").trim();
  } catch {
    return null;
  }
}

const mcp = new Server(
  { name: "autoresume", version: "0.1.0" },
  {
    capabilities: { experimental: { "claude/channel": {} } },
    instructions:
      "A message from the autoresume channel means your rate limit has expired. " +
      "Continue the task you were working on before the interruption.",
  }
);

await mcp.connect(new StdioServerTransport());

const MAX_BODY_BYTES = 1024;
const ALLOW_NO_AUTH = process.env.AUTORESUME_ALLOW_NO_AUTH === "1";

const http = createServer(async (req, res) => {
  if (req.method !== "POST") {
    res.writeHead(405).end("method not allowed");
    return;
  }

  const expectedToken = loadAuthToken();
  if (expectedToken) {
    const auth = req.headers.authorization || "";
    if (auth !== `Bearer ${expectedToken}`) {
      res.writeHead(403).end("forbidden");
      return;
    }
  } else if (!ALLOW_NO_AUTH) {
    process.stderr.write("autoresume: no auth token found — rejecting request (set AUTORESUME_ALLOW_NO_AUTH=1 to override)\n");
    res.writeHead(403).end("forbidden — no auth token configured");
    return;
  }

  try {
    const chunks = [];
    let totalBytes = 0;
    for await (const chunk of req) {
      totalBytes += chunk.length;
      if (totalBytes > MAX_BODY_BYTES) {
        res.writeHead(413).end("body too large");
        return;
      }
      chunks.push(chunk);
    }
    const body = Buffer.concat(chunks).toString().trim();

    if (!body) {
      res.writeHead(400).end("empty body");
      return;
    }

    await mcp.notification({
      method: "notifications/claude/channel",
      params: {
        content: body,
        meta: { source_type: "rate_limit_resume" },
      },
    });

    res.writeHead(200).end("ok");
  } catch (err) {
    process.stderr.write(`autoresume: request failed: ${err.message}\n`);
    res.writeHead(500).end("internal error");
  }
});

http.on("error", (err) => {
  if (err.code === "EADDRINUSE") {
    process.stderr.write(`autoresume: port ${PORT} already in use — another session may have it\n`);
    process.exit(1);
  } else {
    process.stderr.write(`autoresume: ${err.message}\n`);
  }
});

http.listen(PORT, "127.0.0.1", () => {
  process.stderr.write(`autoresume channel listening on http://127.0.0.1:${PORT}\n`);
});
