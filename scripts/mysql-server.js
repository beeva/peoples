#!/usr/bin/env node
"use strict";

/**
 * Start (and stop) this project's own MySQL/MariaDB server.
 *
 *   node scripts/mysql-server.js          # start, stay attached, stop on exit
 *   node scripts/mysql-server.js --stop   # shut a running server down
 *   node scripts/mysql-server.js --status # report whether it is up
 *
 * `npm run dev` / `npm run start` run this alongside the data server and the
 * web app, so nobody has to open the XAMPP control panel by hand.
 *
 * **The database belongs to the project.** Its data directory (`db/data`), its
 * config (`db/my.ini`) and its port (3307) all live here; XAMPP supplies only
 * the `mysqld` binary. So this server and the one behind the XAMPP panel are
 * separate installations that happen to share an executable -- starting or
 * wiping one cannot affect the other, and the port is different so the two can
 * run side by side. The data directory is created on first run.
 *
 * If something is already listening on our port we reuse it and never shut it
 * down on exit: it is not ours to stop. (server.py checks that whatever
 * answered is really serving `db/data`, and says so loudly if it is not.)
 *
 * No dependencies: readiness is checked by reading MySQL's own handshake
 * packet off a plain TCP socket, so no client library is involved.
 */
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");

// ---- configuration --------------------------------------------------------
// Read from .env (same file server.py reads) so the database lives in exactly
// one place; real environment variables win, as everywhere else in this repo.
function loadDotenv(file) {
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch {
    return;
  }
  for (const raw of text.split(/\r?\n/)) {
    let line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    if (line.startsWith("export ")) line = line.slice(7).trimStart();
    const eq = line.indexOf("=");
    if (eq < 0) continue;
    const key = line.slice(0, eq).trim();
    let val = line.slice(eq + 1).trim();
    if (val.length >= 2 && val[0] === val[val.length - 1] && (val[0] === '"' || val[0] === "'")) {
      val = val.slice(1, -1);
    }
    if (key && !(key in process.env)) process.env[key] = val;
  }
}
loadDotenv(path.join(ROOT, ".env"));

// Where XAMPP put MySQL. The default matches this machine; MYSQL_BASEDIR
// overrides it, and the fallbacks cover the usual install drives.
const CANDIDATE_BASEDIRS = [
  process.env.MYSQL_BASEDIR,
  "D:/xampp/mysql",
  "C:/xampp/mysql",
  "E:/xampp/mysql",
].filter(Boolean);

// This project's own database lives inside the project: its data directory,
// its config, its port. XAMPP supplies only the mysqld binary. Nothing here
// touches the databases in your XAMPP install and nothing there touches these.
const DB_DIR = path.join(ROOT, "db");
const DATA_DIR = process.env.MYSQL_DATADIR || path.join(DB_DIR, "data");
const DEFAULTS_FILE = path.join(DB_DIR, "my.ini");

const HOST = process.env.MYSQL_HOST || "127.0.0.1";
// 3307, not 3306, so opening the XAMPP control panel cannot collide with this
// server (or, worse, quietly answer on the port with the wrong data directory).
const PORT = Number(process.env.MYSQL_PORT || 3307);
const USER = process.env.MYSQL_USER || "root";
const PASSWORD = process.env.MYSQL_PASSWORD || "";
// How long to wait for a cold start. InnoDB crash recovery after an unclean
// shutdown is the slow case, so this is generous rather than snappy.
const START_TIMEOUT_MS = Number(process.env.MYSQL_START_TIMEOUT || 60000);
const PID_FILE = path.join(ROOT, ".mysqld.pid");

const isWin = process.platform === "win32";
const exe = (name) => (isWin ? `${name}.exe` : name);

function findBasedir() {
  for (const dir of CANDIDATE_BASEDIRS) {
    if (fs.existsSync(path.join(dir, "bin", exe("mysqld")))) return dir;
  }
  return null;
}

/**
 * Create db/data on first run.
 *
 * A data directory is more than an empty folder -- it holds the `mysql` schema
 * that defines what accounts exist -- so it has to be built by MariaDB's own
 * bootstrap tool rather than by us.
 */
function initDataDir(basedir) {
  if (fs.existsSync(path.join(DATA_DIR, "mysql"))) return true;
  const installer = path.join(basedir, "bin", exe("mysql_install_db"));
  if (!fs.existsSync(installer)) {
    log(`cannot initialise ${DATA_DIR}: ${installer} is missing`);
    return false;
  }
  log(`first run: creating the project database in ${DATA_DIR}`);
  fs.mkdirSync(DATA_DIR, { recursive: true });
  const res = spawnSync(
    installer,
    [`--datadir=${DATA_DIR}`, `--port=${PORT}`, "--default-user", "--silent"],
    { cwd: basedir, stdio: "inherit", windowsHide: true },
  );
  if (res.status !== 0) {
    log(`mysql_install_db failed (exit ${res.status})`);
    return false;
  }
  return true;
}

// `concurrently` already labels every line it forwards, so adding our own
// prefix there would print "[mysql] [mysql] ...". A pipe means someone is
// wrapping us; a TTY means we were run on our own and need the label.
const PREFIX = process.stdout.isTTY ? "[mysql] " : "";

function log(msg) {
  process.stdout.write(`${PREFIX}${msg}\n`);
}

// ---- readiness ------------------------------------------------------------
/**
 * Resolve true when the server answers with a real MySQL handshake.
 *
 * A successful TCP connect is not enough: Windows accepts connections on a
 * socket the moment mysqld binds it, well before the server is willing to
 * authenticate. The initial handshake packet starts with a 3-byte length, a
 * sequence byte, then the protocol version (10) -- seeing that byte is proof
 * the server is actually serving.
 */
function ping(timeoutMs = 1500) {
  return new Promise((resolve) => {
    const sock = new net.Socket();
    let done = false;
    const finish = (ok) => {
      if (done) return;
      done = true;
      sock.destroy();
      resolve(ok);
    };
    sock.setTimeout(timeoutMs);
    sock.once("timeout", () => finish(false));
    sock.once("error", () => finish(false));
    sock.once("data", (buf) => finish(buf.length > 4 && buf[4] === 10));
    sock.connect(PORT, HOST);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Blocking sleep, for the shutdown path where the event loop is already done. */
function sleepSync(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

async function waitUntilReady(deadline, child) {
  while (Date.now() < deadline) {
    if (child && child.exitCode !== null) return false;
    if (await ping()) return true;
    await sleep(300);
  }
  return false;
}

// ---- error reporting ------------------------------------------------------
function tailErrorLog() {
  const candidates = [
    path.join(DATA_DIR, "mysql_error.log"),
    path.join(DATA_DIR, `${os.hostname()}.err`),
  ];
  for (const file of candidates) {
    try {
      const lines = fs.readFileSync(file, "utf8").trim().split(/\r?\n/);
      if (lines.length) return lines.slice(-12).join("\n");
    } catch {
      /* try the next candidate */
    }
  }
  return "";
}

// ---- stop -----------------------------------------------------------------
function readPidFile() {
  try {
    const pid = Number(fs.readFileSync(PID_FILE, "utf8").trim());
    return Number.isInteger(pid) && pid > 0 ? pid : null;
  } catch {
    return null;
  }
}

/**
 * Ask the server to shut itself down, so InnoDB flushes and the next start
 * skips crash recovery. Falls back to killing the pid we recorded.
 */
function shutdown(basedir) {
  const admin = path.join(basedir, "bin", exe("mysqladmin"));
  if (fs.existsSync(admin)) {
    const args = [`--host=${HOST}`, `--port=${PORT}`, `--user=${USER}`];
    if (PASSWORD) args.push(`--password=${PASSWORD}`);
    args.push("--connect-timeout=5", "shutdown");
    const res = spawnSync(admin, args, { stdio: "ignore", windowsHide: true });
    if (res.status === 0) return true;
  }
  const pid = readPidFile();
  if (pid) {
    try {
      if (isWin) spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], { stdio: "ignore" });
      else process.kill(pid, "SIGTERM");
      return true;
    } catch {
      /* already gone */
    }
  }
  return false;
}

// ---- entry points ---------------------------------------------------------
async function cmdStatus(basedir) {
  const up = await ping();
  log(up ? `running on ${HOST}:${PORT}` : `not running on ${HOST}:${PORT}`);
  log(`data: ${DATA_DIR}`);
  if (basedir) log(`mysqld from: ${basedir}`);
  process.exit(up ? 0 : 1);
}

async function cmdStop(basedir) {
  if (!(await ping())) {
    log("already stopped");
    try { fs.unlinkSync(PID_FILE); } catch { /* nothing to clean up */ }
    process.exit(0);
  }
  if (!basedir) {
    log("cannot find mysqladmin -- set MYSQL_BASEDIR to your XAMPP mysql folder");
    process.exit(1);
  }
  shutdown(basedir);
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    if (!(await ping(800))) {
      log("stopped");
      try { fs.unlinkSync(PID_FILE); } catch { /* nothing to clean up */ }
      process.exit(0);
    }
    await sleep(300);
  }
  log("shutdown timed out");
  process.exit(1);
}

async function cmdStart(basedir) {
  // Someone else's server is already there -- use it, but leave it running
  // when we exit. We stay alive so `concurrently -k` does not read our exit
  // as "a task finished" and tear the other processes down with it.
  if (await ping()) {
    // Ours is the only thing that should be on this port. If something
    // else is, say so plainly -- silently serving from a different data
    // directory would look like the archive had vanished.
    log(`a server is already listening on ${HOST}:${PORT} -- reusing it`);
    log(`  (expected data directory: ${DATA_DIR})`);
    idleUntilSignalled(null, null);
    return;
  }

  if (!basedir) {
    log("could not find mysqld. Looked in:");
    for (const d of CANDIDATE_BASEDIRS) log(`  ${d}`);
    log("Set MYSQL_BASEDIR in .env to your XAMPP mysql folder (the one with bin/mysqld.exe).");
    process.exit(1);
  }

  if (!initDataDir(basedir)) process.exit(1);

  const mysqld = path.join(basedir, "bin", exe("mysqld"));
  // `--defaults-file` makes db/my.ini the only config read, so XAMPP's own
  // my.ini cannot leak settings in. The paths and port come after it on the
  // command line, where they win over anything the file says.
  const args = [
    `--defaults-file=${DEFAULTS_FILE}`,
    `--basedir=${basedir}`,
    `--datadir=${DATA_DIR}`,
    `--port=${PORT}`,
    "--console",
  ];
  if (process.env.MYSQL_BUFFER_POOL) {
    args.push(`--innodb-buffer-pool-size=${process.env.MYSQL_BUFFER_POOL}`);
  }
  if (process.env.MYSQL_MAX_PACKET) {
    args.push(`--max-allowed-packet=${process.env.MYSQL_MAX_PACKET}`);
  }

  log(`starting ${path.basename(mysqld)} on ${HOST}:${PORT}`);
  log(`  data: ${DATA_DIR}`);
  const child = spawn(mysqld, args, {
    cwd: basedir,
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });

  // Our readiness probe reads the handshake and hangs up without logging in,
  // which mysqld dutifully reports as an aborted unauthenticated connection.
  // That is us, once every 300ms -- not worth showing anyone.
  const isProbeNoise = (line) =>
    /Aborted connection/.test(line) && /unauthenticated/.test(line);

  // mysqld is chatty on startup; forward it prefixed so it is distinguishable
  // from the data server and Next.js in the shared `concurrently` output.
  const forward = (stream) => {
    let buf = "";
    stream.on("data", (chunk) => {
      buf += chunk.toString();
      const lines = buf.split(/\r?\n/);
      buf = lines.pop() || "";
      for (const line of lines) {
        const text = line.trim();
        if (text && !isProbeNoise(text)) log(text);
      }
    });
  };
  forward(child.stdout);
  forward(child.stderr);

  child.on("error", (err) => {
    log(`could not launch mysqld: ${err.message}`);
    process.exit(1);
  });

  try {
    fs.writeFileSync(PID_FILE, String(child.pid));
  } catch {
    /* the pid file is only a fallback for shutdown; not worth failing over */
  }

  const ready = await waitUntilReady(Date.now() + START_TIMEOUT_MS, child);
  if (!ready) {
    log(`server did not become ready within ${Math.round(START_TIMEOUT_MS / 1000)}s`);
    const tail = tailErrorLog();
    if (tail) log(`last lines of the error log:\n${tail}`);
    try { child.kill(); } catch { /* already dead */ }
    process.exit(1);
  }

  log(`ready on ${HOST}:${PORT}`);
  idleUntilSignalled(child, basedir);
}

/**
 * Keep the process alive until we are asked to quit, then shut the server down
 * if we were the one who started it.
 *
 * `stopping` guards against running the shutdown twice: on Windows a Ctrl+C in
 * a shared console delivers SIGINT to every process in the group, and
 * `concurrently -k` may follow it with its own terminate.
 */
function idleUntilSignalled(child, basedir) {
  let stopping = false;

  const quit = (code) => {
    if (stopping) return;
    stopping = true;
    if (child && basedir) {
      log("shutting the server down");
      shutdown(basedir);
      // Give mysqld a moment to flush before the process group goes away.
      // The wait has to be synchronous -- we are on the way out and the event
      // loop will not get another turn.
      const deadline = Date.now() + 15000;
      while (child.exitCode === null && Date.now() < deadline) sleepSync(200);
      try { fs.unlinkSync(PID_FILE); } catch { /* nothing to clean up */ }
    }
    process.exit(code);
  };

  for (const sig of ["SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"]) {
    try {
      process.on(sig, () => quit(0));
    } catch {
      /* not every signal exists on every platform */
    }
  }
  if (child) {
    child.on("exit", (code) => {
      if (stopping) return;
      log(`server exited (code ${code})`);
      process.exit(code === 0 ? 0 : 1);
    });
  }
  // Nothing else keeps the event loop alive once the child is detached from
  // our stdio, so hold it open explicitly.
  setInterval(() => {}, 1 << 30);
}

async function main() {
  const basedir = findBasedir();
  const arg = process.argv[2] || "";
  if (arg === "--stop" || arg === "stop") return cmdStop(basedir);
  if (arg === "--status" || arg === "status") return cmdStatus(basedir);
  return cmdStart(basedir);
}

main().catch((err) => {
  log(String((err && err.stack) || err));
  process.exit(1);
});
