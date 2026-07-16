#!/usr/bin/env node
"use strict";

/**
 * Kill whatever process is LISTENING on the given TCP port(s).
 *
 *   node scripts/kill-port.js 8000          # kill the data server
 *   node scripts/kill-port.js 8000 3090     # data server + web dev server
 *
 * Cross-platform (Windows / macOS / Linux), no dependencies. Only listeners
 * are targeted, so client connections (e.g. your browser) are left alone.
 * Exits 0 even if nothing was found, so it is safe to chain before `dev`.
 */
const { execSync } = require("child_process");

const isWin = process.platform === "win32";
const ports = process.argv.slice(2).filter(Boolean);
if (ports.length === 0) ports.push("8000");

function pidsOnPort(port) {
  try {
    if (isWin) {
      const out = execSync("netstat -ano -p tcp", { encoding: "utf8" });
      const pids = new Set();
      for (const line of out.split(/\r?\n/)) {
        // Proto  Local Address  Foreign Address  State      PID
        const m = line.match(/^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)/i);
        if (m && m[1] === String(port)) pids.add(m[2]);
      }
      return [...pids];
    }
    const out = execSync(`lsof -ti tcp:${port} -sTCP:LISTEN`, { encoding: "utf8" });
    return out.split(/\s+/).filter(Boolean);
  } catch {
    return []; // nothing listening / command returned non-zero
  }
}

function kill(pid) {
  try {
    if (isWin) execSync(`taskkill /F /PID ${pid}`, { stdio: "ignore" });
    else execSync(`kill -9 ${pid}`, { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

for (const port of ports) {
  const pids = pidsOnPort(port);
  if (pids.length === 0) {
    console.log(`port ${port}: nothing listening`);
    continue;
  }
  for (const pid of pids) {
    console.log(`port ${port}: ${kill(pid) ? "killed" : "could not kill"} pid ${pid}`);
  }
}

process.exit(0);
