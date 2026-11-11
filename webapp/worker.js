/*
 * Personal Podcasts — multi-user web app backend (Cloudflare Worker).
 *
 * One Worker serves the SPA (static assets in public/) and the /api JSON API.
 * It is the only component that holds the GitHub token: profiles stay in the
 * repo (config/profiles/<name>.json) as the source of truth, with show
 * ownership as an "owner" field inside each profile. Users, invites, and
 * rate-limit counters live in Workers KV. Sessions are stateless HMAC tokens.
 *
 * Python (config_loader.py / trace.py) remains the validation authority; the
 * PY_PARITY blob below is a mirror, drift-guarded by tests/test_admin_page.py.
 * The Worker rejects any profile write that fails the mirrored checks, so a
 * web-app write can never break load_podcast_profile. It does NOT enforce the
 * publish gate (assert_publishable) on save — drafts are allowed, and the
 * pipeline gate stays the single publish authority.
 *
 * Zero dependencies, no build step. Pure helpers are exported for
 * `node --test webapp/`.
 */

/* py-parity */ const PY_PARITY = {
  "PROFILE_DEFAULTS": {
    "scope": "regional",
    "cadence": "dialog",
    "depth": "deep_dive",
    "length_minutes": 15,
    "schedule": "weekly",
    "schedule_day": "sunday",
    "voices": "cariboo_signals",
    "psa": false
  },
  "PROFILE_ENUMS": {
    "scope": ["global", "regional"],
    "cadence": ["dialog", "news"],
    "depth": ["deep_dive", "titles"],
    "schedule": ["daily", "weekly"]
  },
  "WEEKDAYS": ["friday", "monday", "saturday", "sunday", "thursday", "tuesday", "wednesday"],
  "LENGTH_RANGE": [5, 30],
  "KITE_MAX_CATEGORIES": 8,
  "TRACE_CATEGORIES": {
    "transparency": 4,
    "sourceIntegrity": 3,
    "displacement": 2,
    "consentAttribution": 4,
    "benefitFlow": 5,
    "accountability": 2
  },
  "TRACE_TOTAL_MAX": 20,
  "TRACE_REQUIRED_FIELDS": ["producer_url", "community", "ai_role", "ai_tools",
                            "track", "assessment_date", "assessed_by", "scores"],
  "VERDICT_THRESHOLDS": [[85, "Exemplary"], [70, "Strong"], [50, "Adequate"], [0, "Weak"]]
}; /* end py-parity */

// Show names become repo paths (config/profiles/<name>.json) and are the ONLY
// user input ever interpolated into a GitHub contents-API URL — gate first.
const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const USERNAME_RE = /^[a-z0-9][a-z0-9_-]{2,31}$/;
const PIN_MIN_LENGTH = 6;
const PBKDF2_ITERATIONS = 100000; // Workers runtime cap for PBKDF2
const TOKEN_TTL_S = 7 * 24 * 3600;
const INVITE_TTL_S = 7 * 24 * 3600;
const LIMITS = {
  login_ip: { max: 10, ttl: 900 },
  login_user: { max: 5, ttl: 900 },
  join_ip: { max: 5, ttl: 3600 },
  generate_daily: { max: 4, ttl: 86400 }, // per non-admin user — API cost discipline
};

// ── Encoding helpers ────────────────────────────────────────────────────────

const te = new TextEncoder();

function bytesToB64(bytes) {
  let bin = "";
  for (const b of new Uint8Array(bytes)) bin += String.fromCharCode(b);
  return btoa(bin);
}

function b64ToBytes(b64) {
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

function bytesToB64url(bytes) {
  return bytesToB64(bytes).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlToBytes(s) {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - s.length % 4) % 4);
  return b64ToBytes(b64);
}

// ── Crypto: PIN hashing, session tokens ─────────────────────────────────────

export async function pbkdf2Hash(pin, saltB64, iterations = PBKDF2_ITERATIONS) {
  const key = await crypto.subtle.importKey("raw", te.encode(pin), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt: b64ToBytes(saltB64), iterations },
    key, 256);
  return bytesToB64(bits);
}

async function hmac(secret, message) {
  const key = await crypto.subtle.importKey(
    "raw", te.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, te.encode(message)));
}

// Constant-time-ish equality: compare random-keyed HMACs of both values so the
// byte comparison timing carries no information about the secrets themselves.
export async function safeEqual(a, b) {
  const key = bytesToB64(crypto.getRandomValues(new Uint8Array(32)));
  const [ha, hb] = await Promise.all([hmac(key, a), hmac(key, b)]);
  if (ha.length !== hb.length) return false;
  let diff = 0;
  for (let i = 0; i < ha.length; i++) diff |= ha[i] ^ hb[i];
  return diff === 0;
}

// Token: "v1.<b64url(payload)>.<b64url(hmac)>", payload {u, tv, exp}.
// Role and revocation state are always re-read from KV, never trusted here.
export async function signToken(payload, secret) {
  const body = bytesToB64url(te.encode(JSON.stringify(payload)));
  const sig = bytesToB64url(await hmac(secret, "v1." + body));
  return `v1.${body}.${sig}`;
}

export async function verifyToken(token, secret, now = Date.now()) {
  const parts = (token || "").split(".");
  if (parts.length !== 3 || parts[0] !== "v1") return null;
  const expected = bytesToB64url(await hmac(secret, "v1." + parts[1]));
  if (!(await safeEqual(parts[2], expected))) return null;
  let payload;
  try {
    payload = JSON.parse(new TextDecoder().decode(b64urlToBytes(parts[1])));
  } catch {
    return null;
  }
  if (typeof payload.exp !== "number" || payload.exp * 1000 < now) return null;
  return payload;
}

export function randomCode() {
  // 128-bit invite/join code, base32 (no confusable chars beats base64 in URLs).
  const alphabet = "abcdefghjkmnpqrstuvwxyz23456789";
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

// ── Profile validation (mirror of config_loader.load_podcast_profile +
//    trace.validate_trace structural checks; Python stays the authority) ─────

export function validName(name) {
  return NAME_RE.test(name || "");
}

export function validUsername(username) {
  return USERNAME_RE.test(username || "");
}

// Mirror of config_loader._validate_kite_sources (structural checks; the
// known-name check stays in Python, the picker only offers Kite's own names).
export function kiteSourceErrors(kite) {
  if (kite === undefined || kite === null) return [];
  if (!Array.isArray(kite) || !kite.every((c) => typeof c === "string" && c.trim())) {
    return ["sources.kite must be a list of category names"];
  }
  const errors = [];
  if (new Set(kite).size !== kite.length) errors.push("sources.kite has duplicate categories");
  if (kite.length > PY_PARITY.KITE_MAX_CATEGORIES) {
    errors.push(`sources.kite has ${kite.length} categories (max ${PY_PARITY.KITE_MAX_CATEGORIES})`);
  }
  return errors;
}

export function validateProfile(profile) {
  const P = PY_PARITY;
  const errors = [];
  if (!profile || typeof profile !== "object" || Array.isArray(profile)) {
    return ["profile must be a JSON object"];
  }
  const merged = { ...P.PROFILE_DEFAULTS, ...profile };
  for (const [field, allowed] of Object.entries(P.PROFILE_ENUMS)) {
    if (!allowed.includes(merged[field])) {
      errors.push(`${field}=${JSON.stringify(merged[field])} must be one of ${allowed.join(", ")}`);
    }
  }
  const len = merged.length_minutes;
  if (!Number.isInteger(len) || len < P.LENGTH_RANGE[0] || len > P.LENGTH_RANGE[1]) {
    errors.push(`length_minutes=${len} out of range ${P.LENGTH_RANGE[0]}–${P.LENGTH_RANGE[1]}`);
  }
  if (merged.schedule === "weekly" && !P.WEEKDAYS.includes(merged.schedule_day)) {
    errors.push(`schedule_day=${JSON.stringify(merged.schedule_day)} must be a weekday name`);
  }

  errors.push(...kiteSourceErrors((merged.sources || {}).kite));

  const t = merged.trace;
  if (!t || typeof t !== "object" || Array.isArray(t)) {
    errors.push("profile is missing a required 'trace' block (TRACE standard)");
    return errors;
  }
  const missing = P.TRACE_REQUIRED_FIELDS.filter((f) => !(f in t));
  if (missing.length) errors.push(`trace block missing required fields: ${missing.join(", ")}`);
  const scores = t.scores;
  if (scores && typeof scores === "object" && !Array.isArray(scores)) {
    const want = Object.keys(P.TRACE_CATEGORIES).sort();
    const got = Object.keys(scores).sort();
    // Exact key set, like trace.validate_trace — extra keys are rejected too.
    if (JSON.stringify(want) !== JSON.stringify(got)) {
      errors.push(`trace scores must cover exactly ${want.join(", ")}; got ${got.join(", ") || "none"}`);
    } else {
      for (const [cat, max] of Object.entries(P.TRACE_CATEGORIES)) {
        const raw = scores[cat];
        const value = raw && typeof raw === "object" ? raw.score : raw;
        if (!Number.isInteger(value) || value < 0 || value > max) {
          errors.push(`trace score '${cat}'=${JSON.stringify(value)} out of range 0–${max}`);
        }
      }
    }
  } else if (!missing.includes("scores")) {
    errors.push("trace scores must be an object");
  }
  return errors;
}

// ── Small response helpers ──────────────────────────────────────────────────

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

function apiError(status, error) {
  return json({ error }, status);
}

// ── KV: users, invites, rate limits ─────────────────────────────────────────

async function getUser(env, username) {
  return env.PP_KV.get(`user:${username}`, "json");
}

// Approximate fixed-window counter: each hit refreshes the TTL, which only
// makes the limit stricter. KV is eventually consistent — good enough at
// friends-scale, and it fails toward blocking, not toward abuse.
async function overLimit(env, key, { max, ttl }) {
  const count = parseInt((await env.PP_KV.get(`rl:${key}`)) || "0", 10);
  if (count >= max) return true;
  await env.PP_KV.put(`rl:${key}`, String(count + 1), { expirationTtl: ttl });
  return false;
}

function clientIp(request) {
  return request.headers.get("CF-Connecting-IP") || "unknown";
}

// ── GitHub API (the token never leaves the Worker) ──────────────────────────

async function gh(env, path, opts = {}) {
  const res = await fetch("https://api.github.com" + path, {
    ...opts,
    headers: {
      "Authorization": "Bearer " + env.GITHUB_TOKEN,
      "Accept": "application/vnd.github+json",
      "User-Agent": "personal-podcasts-webapp",
      ...(opts.body ? { "Content-Type": "application/json" } : {}),
    },
  });
  if (res.status === 204) return null;
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(body.message || `GitHub ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return body;
}

function profilePath(name) {
  return `/contents/config/profiles/${name}.json`;
}

async function fetchProfileFile(env, name) {
  try {
    const file = await gh(env, `/repos/${env.GH_OWNER}/${env.GH_REPO}${profilePath(name)}`);
    const text = new TextDecoder().decode(b64ToBytes(file.content.replace(/\s/g, "")));
    return { data: JSON.parse(text), sha: file.sha };
  } catch (err) {
    if (err.status === 404) return null;
    throw err;
  }
}

async function lastEpisodeDate(env, name) {
  try {
    const files = await gh(env, `/repos/${env.GH_OWNER}/${env.GH_REPO}/contents/shows/${name}/podcasts`);
    const dates = files
      .map((f) => (f.name.match(/^citations_(\d{4}-\d{2}-\d{2})\.json$/) || [])[1])
      .filter(Boolean)
      .sort();
    return dates.pop() || null;
  } catch (err) {
    if (err.status === 404) return null;
    throw err;
  }
}

// Ownership: profiles without an "owner" field predate the web app and belong
// to the admin (grandfathering); the admin can manage everything.
function canManage(user, profileData) {
  if (user.role === "admin") return true;
  return profileData.owner === user.username;
}

// ── Auth middleware ─────────────────────────────────────────────────────────

async function requireUser(request, env) {
  const auth = request.headers.get("Authorization") || "";
  if (!auth.startsWith("Bearer ")) return null;
  const payload = await verifyToken(auth.slice(7), env.SESSION_SECRET);
  if (!payload) return null;
  const user = await getUser(env, payload.u);
  if (!user || user.token_version !== payload.tv) return null; // revoked/deleted
  return user;
}

async function issueToken(env, user) {
  return signToken(
    { u: user.username, tv: user.token_version, exp: Math.floor(Date.now() / 1000) + TOKEN_TTL_S },
    env.SESSION_SECRET);
}

function publicUser(user) {
  return { username: user.username, display_name: user.display_name, role: user.role };
}

// ── Route handlers ──────────────────────────────────────────────────────────

async function handleJoin(request, env) {
  if (await overLimit(env, `join:${clientIp(request)}`, LIMITS.join_ip)) {
    return apiError(429, "Too many join attempts — try again later.");
  }
  const body = await request.json().catch(() => null);
  if (!body) return apiError(400, "Invalid JSON body.");
  const { invite_code, username, display_name, pin } = body;

  if (!validUsername(username)) {
    return apiError(400, "Username must be 3–32 chars: lowercase letters, digits, - or _.");
  }
  if (typeof pin !== "string" || pin.length < PIN_MIN_LENGTH) {
    return apiError(400, `PIN/passphrase must be at least ${PIN_MIN_LENGTH} characters.`);
  }
  const invite = await env.PP_KV.get(`invite:${invite_code}`, "json");
  if (!invite || (invite.expires_at && Date.parse(invite.expires_at) < Date.now())) {
    return apiError(403, "Invalid or expired invite code.");
  }
  if (await getUser(env, username)) return apiError(409, "That username is taken.");

  const salt = bytesToB64(crypto.getRandomValues(new Uint8Array(16)));
  const user = {
    username,
    display_name: (display_name || username).slice(0, 80),
    pin_hash: await pbkdf2Hash(pin, salt),
    salt,
    iterations: PBKDF2_ITERATIONS,
    role: invite.role === "admin" ? "admin" : "user",
    token_version: 1,
    created_at: new Date().toISOString(),
    invited_by: invite.created_by || "",
  };
  await env.PP_KV.put(`user:${username}`, JSON.stringify(user));
  await env.PP_KV.delete(`invite:${invite_code}`); // single-use
  return json({ token: await issueToken(env, user), user: publicUser(user) });
}

async function handleLogin(request, env) {
  const body = await request.json().catch(() => null);
  if (!body) return apiError(400, "Invalid JSON body.");
  const { username, pin } = body;
  const ip = clientIp(request);
  if (await overLimit(env, `login:ip:${ip}`, LIMITS.login_ip) ||
      (validUsername(username) && await overLimit(env, `login:u:${username}`, LIMITS.login_user))) {
    return apiError(429, "Too many login attempts — try again in 15 minutes.");
  }
  const user = validUsername(username) ? await getUser(env, username) : null;
  // Hash even for unknown users so response timing doesn't reveal existence.
  const salt = user ? user.salt : bytesToB64(new Uint8Array(16));
  const hash = await pbkdf2Hash(String(pin || ""), salt, user ? user.iterations : PBKDF2_ITERATIONS);
  if (!user || !(await safeEqual(hash, user.pin_hash))) {
    return apiError(401, "Wrong username or PIN.");
  }
  return json({ token: await issueToken(env, user), user: publicUser(user) });
}

async function handleListProfiles(env, user) {
  let listing;
  try {
    listing = await gh(env, `/repos/${env.GH_OWNER}/${env.GH_REPO}/contents/config/profiles`);
  } catch (err) {
    if (err.status === 404) return json({ profiles: [] });
    throw err;
  }
  const names = listing
    .filter((f) => f.name.endsWith(".json"))
    .map((f) => f.name.replace(/\.json$/, ""))
    .sort();
  const results = await Promise.all(names.map(async (name) => {
    const file = await fetchProfileFile(env, name);
    if (!file || !canManage(user, file.data)) return null;
    return { name, data: file.data, last_episode: await lastEpisodeDate(env, name) };
  }));
  return json({ profiles: results.filter(Boolean) });
}

async function handleGetProfile(env, user, name) {
  const file = await fetchProfileFile(env, name);
  if (!file || !canManage(user, file.data)) return apiError(404, "No such show.");
  return json({ name, data: file.data });
}

async function handlePutProfile(request, env, user, name) {
  const profile = await request.json().catch(() => null);
  if (!profile) return apiError(400, "Invalid JSON body.");
  delete profile.name; // injected by config_loader at load time, never stored

  const existing = await fetchProfileFile(env, name);
  if (existing && !canManage(user, existing.data)) {
    return apiError(404, "No such show."); // don't reveal other users' shows
  }
  // Ownership is server-assigned: non-admins can never write someone else's
  // name into "owner"; admins keep whatever the file/body says.
  if (user.role !== "admin") {
    profile.owner = user.username;
  } else if (!profile.owner) {
    profile.owner = existing ? (existing.data.owner || user.username) : user.username;
  }
  // The SPA doesn't know where shows publish; default the public URLs here,
  // matching the old admin page's `${PAGES_ROOT}shows/<name>/` convention.
  if (!profile.base_url) profile.base_url = `${env.PAGES_ROOT}shows/${name}/`;
  if (profile.trace && !profile.trace.producer_url) profile.trace.producer_url = env.PAGES_ROOT;

  const errors = validateProfile(profile);
  if (errors.length) return json({ error: "Profile failed validation.", errors }, 400);

  const body = {
    message: `${existing ? "Update" : "Add"} profile: ${name} (web app, ${user.username})`,
    content: bytesToB64(te.encode(JSON.stringify(profile, null, 2) + "\n")),
    branch: env.GH_BRANCH,
  };
  if (existing) body.sha = existing.sha;
  try {
    await gh(env, `/repos/${env.GH_OWNER}/${env.GH_REPO}${profilePath(name)}`,
             { method: "PUT", body: JSON.stringify(body) });
  } catch (err) {
    if (err.status === 409 || err.status === 422) {
      return apiError(409, "The show changed since you loaded it — reload and re-edit.");
    }
    throw err;
  }
  return json({ ok: true, name, data: profile });
}

async function handleDeleteProfile(env, user, name) {
  const existing = await fetchProfileFile(env, name);
  if (!existing || !canManage(user, existing.data)) return apiError(404, "No such show.");
  await gh(env, `/repos/${env.GH_OWNER}/${env.GH_REPO}${profilePath(name)}`, {
    method: "DELETE",
    body: JSON.stringify({
      message: `Delete profile: ${name} (web app, ${user.username})`,
      sha: existing.sha,
      branch: env.GH_BRANCH,
    }),
  });
  // ponytail: generated shows/<name>/ artifacts are left for manual git
  // cleanup rather than multi-file deletion through the contents API.
  return json({ ok: true });
}

async function handleGenerate(request, env, user, name) {
  const existing = await fetchProfileFile(env, name);
  if (!existing || !canManage(user, existing.data)) return apiError(404, "No such show.");
  if (user.role !== "admin") {
    const day = new Date().toISOString().slice(0, 10);
    if (await overLimit(env, `gen:${user.username}:${day}`, LIMITS.generate_daily)) {
      return apiError(429, `Daily generation limit reached (${LIMITS.generate_daily.max}/day) — episodes cost real API budget.`);
    }
  }
  const body = await request.json().catch(() => ({}));
  await gh(env, `/repos/${env.GH_OWNER}/${env.GH_REPO}/actions/workflows/${env.WORKFLOW_FILE}/dispatches`, {
    method: "POST",
    body: JSON.stringify({
      ref: env.GH_BRANCH,
      inputs: { profile: name, force: body.force ? "true" : "false" },
    }),
  });
  return json({ ok: true });
}

async function handleFeed(env, user, name) {
  const existing = await fetchProfileFile(env, name);
  if (!existing || !canManage(user, existing.data)) return apiError(404, "No such show.");
  // Proxied because github.io sends no CORS headers. URL is built from env
  // constants + the gated name — never from the profile's own base_url.
  const res = await fetch(`${env.PAGES_ROOT}shows/${name}/feed.xml`);
  if (!res.ok) return apiError(404, "No published feed yet — generate an episode first.");
  return new Response(await res.text(), {
    headers: { "Content-Type": "application/xml; charset=utf-8" },
  });
}

async function handleInvites(request, env, user, method, code) {
  if (method === "POST") {
    const newCode = randomCode();
    const invite = {
      role: "user", // API-created invites are never admin; bootstrap by hand in KV
      created_by: user.username,
      created_at: new Date().toISOString(),
      expires_at: new Date(Date.now() + INVITE_TTL_S * 1000).toISOString(),
    };
    await env.PP_KV.put(`invite:${newCode}`, JSON.stringify(invite), { expirationTtl: INVITE_TTL_S });
    const origin = new URL(request.url).origin;
    return json({ code: newCode, join_url: `${origin}/?invite=${newCode}`, ...invite });
  }
  if (method === "DELETE") {
    if (!code) return apiError(400, "Missing invite code.");
    await env.PP_KV.delete(`invite:${code}`);
    return json({ ok: true });
  }
  const list = await env.PP_KV.list({ prefix: "invite:" });
  const invites = await Promise.all(list.keys.map(async (k) => ({
    code: k.name.slice("invite:".length),
    ...(await env.PP_KV.get(k.name, "json")),
  })));
  return json({ invites });
}

async function handleUsers(env, user, method, username) {
  if (method === "DELETE") {
    if (!validUsername(username)) return apiError(400, "Invalid username.");
    if (username === user.username) return apiError(400, "You cannot delete yourself.");
    await env.PP_KV.delete(`user:${username}`);
    // Their profiles keep the orphaned owner string and stay admin-manageable.
    return json({ ok: true });
  }
  const list = await env.PP_KV.list({ prefix: "user:" });
  const users = await Promise.all(list.keys.map(async (k) => {
    const u = await env.PP_KV.get(k.name, "json");
    return u && { ...publicUser(u), created_at: u.created_at, invited_by: u.invited_by };
  }));
  return json({ users: users.filter(Boolean) });
}

// ── Router ──────────────────────────────────────────────────────────────────

async function route(request, env, url) {
  const method = request.method;
  const seg = url.pathname.replace(/^\/api\/?/, "").split("/").filter(Boolean);

  // Same-origin app, no CORS headers emitted anywhere; belt-and-suspenders
  // reject cross-site mutations outright.
  if (method !== "GET" && request.headers.get("Sec-Fetch-Site") === "cross-site") {
    return apiError(403, "Cross-site requests are not allowed.");
  }

  if (seg[0] === "health" && method === "GET") return json({ ok: true });
  if (seg[0] === "join" && method === "POST") return handleJoin(request, env);
  if (seg[0] === "login" && method === "POST") return handleLogin(request, env);

  const user = await requireUser(request, env);
  if (!user) return apiError(401, "Not signed in.");

  if (seg[0] === "me" && method === "GET") return json({ user: publicUser(user) });

  if (seg[0] === "profiles") {
    if (seg.length === 1 && method === "GET") return handleListProfiles(env, user);
    const name = seg[1];
    if (!validName(name)) return apiError(400, "Invalid show name.");
    if (seg.length === 2) {
      if (method === "GET") return handleGetProfile(env, user, name);
      if (method === "PUT") return handlePutProfile(request, env, user, name);
      if (method === "DELETE") return handleDeleteProfile(env, user, name);
    }
    if (seg.length === 3 && seg[2] === "generate" && method === "POST") {
      return handleGenerate(request, env, user, name);
    }
  }

  if (seg[0] === "feed" && seg.length === 2 && method === "GET") {
    if (!validName(seg[1])) return apiError(400, "Invalid show name.");
    return handleFeed(env, user, seg[1]);
  }

  if (seg[0] === "invites" || seg[0] === "users") {
    if (user.role !== "admin") return apiError(403, "Admin only.");
    if (seg[0] === "invites") return handleInvites(request, env, user, method, seg[1]);
    return handleUsers(env, user, method, seg[1]);
  }

  return apiError(404, "No such endpoint.");
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/api/")) {
      // run_worker_first routes only /api/* here, but keep a fallback for dev.
      return env.ASSETS ? env.ASSETS.fetch(request) : new Response("Not found", { status: 404 });
    }
    try {
      return await route(request, env, url);
    } catch (err) {
      // GitHub error details stay in the log; clients get a generic message.
      console.error(err.stack || String(err));
      return apiError(502, "Upstream error — try again.");
    }
  },
};

export { PY_PARITY, NAME_RE, USERNAME_RE, PBKDF2_ITERATIONS };
