#!/usr/bin/env node
/**
 * One-way channel MCP server for claude-autoresume.
 *
 * Listens on a local HTTP port. The daemon POSTs a resume prompt
 * when a rate limit expires, and this server forwards it into the
 * running Claude Code session as a channel notification.
 */

import { createServer } from "node:http";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";

const PORT = parseInt(process.env.AUTORESUME_PORT || "18963", 10);

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

const http = createServer(async (req, res) => {
  if (req.method !== "POST") {
    res.writeHead(405).end("method not allowed");
    return;
  }

  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const body = Buffer.concat(chunks).toString();

  await mcp.notification({
    method: "notifications/claude/channel",
    params: {
      content: body,
      meta: { source_type: "rate_limit_resume" },
    },
  });

  res.writeHead(200).end("ok");
});

http.on("error", (err) => {
  if (err.code === "EADDRINUSE") {
    process.stderr.write(`autoresume: port ${PORT} already in use — another session may have it\n`);
  } else {
    process.stderr.write(`autoresume: ${err.message}\n`);
  }
});

http.listen(PORT, "127.0.0.1", () => {
  process.stderr.write(`autoresume channel listening on http://127.0.0.1:${PORT}\n`);
});
