/**
 * Platform client for a TypeScript agent — this is the Pi side.
 *
 * Pi is a toolkit you embed, not a daemon with a plugin slot, so there is
 * nothing to install into it: your Node service owns the agent loop
 * (@earendil-works/pi-agent-core) and uses this module to appear on the
 * platform, take work and answer.
 *
 *   const pi = new Participant({
 *     name: "Pi Agent", owner: "Kevin", platform: "http://host:9100",
 *     skills: [{ id: "support.frontline", tags: ["support", "refund"] }],
 *   });
 *
 *   await pi.serveTickets(async (ask, ctx) => {
 *     const answer = await myPiAgent.run(ask);        // your agent loop
 *     return answer;                                  // → said in the room
 *   });
 *
 * Identity is the same Ed25519 / did:key scheme the Python side uses: the
 * key file format, the canonical JSON and the signed envelope all match, so a
 * TS agent and a Python agent are indistinguishable to the platform.
 *
 * Run standalone:
 *   node --experimental-strip-types client.ts --name "Pi Agent" --owner Kevin
 */
import { createPrivateKey, generateKeyPairSync, sign as edSign, type KeyObject } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { dirname } from "node:path";

const ED25519_MULTICODEC = Buffer.from([0xed, 0x01]);
const PKCS8_ED25519_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex");

export const b64u = (raw: Buffer): string =>
  raw.toString("base64url").replace(/=+$/, "");

/** Sorted keys, no whitespace, raw UTF-8 — byte-identical to the Python side. */
export function canonical(value: unknown): Buffer {
  const stable = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(stable);
    if (v && typeof v === "object") {
      return Object.fromEntries(
        Object.keys(v as object).sort().map((k) => [k, stable((v as Record<string, unknown>)[k])]),
      );
    }
    return v;
  };
  return Buffer.from(JSON.stringify(stable(value)), "utf8");
}

export interface SkillSpec {
  id: string;
  name?: string;
  description?: string;
  tags?: string[];
  sensitivity?: string;
  examples?: string[];
}

const fullSkill = (s: SkillSpec) => ({
  id: s.id,
  name: s.name ?? s.id,
  description: s.description ?? "",
  tags: s.tags ?? [],
  sensitivity: s.sensitivity ?? "public",
  examples: s.examples ?? [],
});

/** An Ed25519 keypair with a did:key identifier, stored like Python's. */
export class Identity {
  readonly label: string;
  readonly did: string;
  private readonly key: KeyObject;

  private constructor(label: string, seed: Buffer) {
    this.label = label;
    this.key = createPrivateKey({
      key: Buffer.concat([PKCS8_ED25519_PREFIX, seed]),
      format: "der",
      type: "pkcs8",
    });
    const jwk = this.key.export({ format: "jwk" }) as { x: string };
    this.did = "did:key:z" + b64u(Buffer.concat([ED25519_MULTICODEC,
                                                 Buffer.from(jwk.x, "base64url")]));
  }

  static loadOrCreate(label: string, path: string): Identity {
    if (existsSync(path)) {
      const saved = JSON.parse(readFileSync(path, "utf8")) as { seed: string };
      return new Identity(label, Buffer.from(saved.seed, "base64url"));
    }
    const { privateKey } = generateKeyPairSync("ed25519");
    const jwk = privateKey.export({ format: "jwk" }) as { d: string };
    const seed = Buffer.from(jwk.d, "base64url");
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, JSON.stringify({ label, seed: b64u(seed) }), { mode: 0o600 });
    return new Identity(label, seed);
  }

  sign(payload: unknown): string {
    return b64u(edSign(null, canonical(payload), this.key));
  }
}

function buildCard(opts: {
  name: string;
  agent: Identity;
  owner: Identity;
  url: string;
  skills: SkillSpec[];
  description: string;
}) {
  const now = Math.floor(Date.now() / 1000);
  const credClaims = {
    type: "AgentDelegationCredential",
    issuer: opts.owner.did,
    subject: opts.agent.did,
    owner_label: opts.owner.label,
    scopes: opts.skills.map((s) => s.id).sort(),
    issued_at: now,
    expires_at: now + 90 * 86400,
  };
  const delegation = { ...credClaims, proof: opts.owner.sign(credClaims) };
  const claims = {
    name: opts.name,
    did: opts.agent.did,
    url: opts.url,
    description: opts.description,
    version: "0.1.0",
    protocol_version: "0.2",
    transports: ["jsonrpc-http"],
    skills: opts.skills.map(fullSkill).sort((a, b) => (a.id < b.id ? -1 : 1)),
    owner: { label: opts.owner.label, did: opts.owner.did },
    delegation,
  };
  return { ...claims, proof: opts.agent.sign(claims) };
}

export interface Ctx {
  roomId: string;
  topic: string;
  customer: string;
  askedBy: string;
  askedByHuman: string;
  from: "customer" | "agent";
  openCall: boolean;
  /** The turn we are reacting to was itself a follow-up question. */
  needsInput: boolean;
}
/**
 * A string is said to the room; an object lets you address or escalate it.
 * `kind: "error"` means you tried and could not — it settles the ask without
 * being filed as an answer and the customer never sees it. `flagHuman` puts
 * the room in front of a person.
 */
export type Reply = string | {
  text: string; to?: string; status?: string;
  kind?: "say" | "error" | "notice"; flagHuman?: boolean;
  /** This reply is a follow-up question: relayed, never filed as an answer. */
  needsInput?: boolean;
} | null;
export type Handler = (ask: string, ctx: Ctx) => Promise<Reply> | Reply;

export class Participant {
  readonly card: ReturnType<typeof buildCard>;
  readonly name: string;
  private readonly agent: Identity;
  private readonly platform: string;
  private readonly skills: SkillSpec[];
  private readonly poll: number;

  constructor(opts: {
    name: string;
    owner: string;
    platform?: string;
    skills?: SkillSpec[];
    keyDir?: string;
    description?: string;
    poll?: number;
  }) {
    this.name = opts.name;
    this.platform = (opts.platform ?? "http://127.0.0.1:9100").replace(/\/$/, "");
    this.skills = opts.skills ?? [];
    this.poll = opts.poll ?? 20;
    const slug = opts.name.toLowerCase().replace(/\s+/g, "-");
    const keys = opts.keyDir ?? `data/keys/${slug}`;
    const owner = Identity.loadOrCreate(opts.owner, `${keys}/owner.json`);
    this.agent = Identity.loadOrCreate(opts.name, `${keys}/agent.json`);
    this.card = buildCard({
      name: opts.name, agent: this.agent, owner, url: `${this.platform}/rooms`,
      skills: this.skills,
      description: opts.description ?? `${opts.owner}'s ${opts.name}`,
    });
  }

  get did(): string {
    return this.card.did;
  }

  get tags(): Set<string> {
    const words = new Set<string>();
    for (const s of this.skills) {
      for (const t of s.tags ?? []) words.add(t.toLowerCase());
      for (const part of s.id.toLowerCase().split(".")) words.add(part);
    }
    return words;
  }

  wants(text: string): boolean {
    const lowered = text.toLowerCase();
    return [...this.tags].some((tag) => lowered.includes(tag));
  }

  // ------------------------------------------------------------- transport
  private envelope(method: string, params: unknown) {
    const rpc = { jsonrpc: "2.0", id: `rpc_${Date.now().toString(36)}`, method, params };
    const issuedAt = Math.floor(Date.now() / 1000);
    const payload = { callerDid: this.did, issuedAt, rpc };
    return {
      callerDid: this.did, callerCard: this.card, issuedAt, rpc,
      signature: this.agent.sign(payload),
    };
  }

  private async post(path: string, method: string, params: unknown = {}) {
    const resp = await fetch(`${this.platform}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(this.envelope(method, params)),
    });
    if (!resp.ok) throw new Error(`${path} -> ${resp.status}: ${await resp.text()}`);
    return resp.json();
  }

  private async get(path: string, query: Record<string, string | number> = {}) {
    const url = new URL(`${this.platform}${path}`);
    for (const [k, v] of Object.entries(query)) url.searchParams.set(k, String(v));
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`${path} -> ${resp.status}: ${await resp.text()}`);
    return resp.json();
  }

  // ------------------------------------------------------------- platform ops
  async register(): Promise<void> {
    await fetch(`${this.platform}/agents`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(this.card),
    });
    await fetch(`${this.platform}/agents/${this.did}/heartbeat`, { method: "POST" });
  }

  join = (roomId: string) => this.post(`/rooms/${roomId}/join`, "room/join");

  say = (roomId: string, text: string,
         opts: { to?: string; status?: string; kind?: string;
                 flagHuman?: boolean; needsInput?: boolean } = {}) =>
    this.post(`/rooms/${roomId}/utterances`, "room/post",
      { text, to: opts.to ?? "", kind: opts.kind ?? "say",
        status: opts.status ?? "", flag_human: opts.flagHuman ?? false,
        needs_input: opts.needsInput ?? false });

  claim = (roomId: string, utteranceId: string) =>
    this.post(`/rooms/${roomId}/openings/${utteranceId}/claim`, "room/claim");

  read = (roomId: string, since = 0, wait = 0) =>
    this.get(`/rooms/${roomId}`, { since, wait }) as Promise<any>;

  tickets = async () => ((await this.get("/rooms", { kind: "ticket" })) as any).rooms as any[];

  mentions = async (wait = 0) =>
    ((await this.get("/mentions", { name: this.name, wait })) as any).mentions as any[];

  openings = async (wait = 0) =>
    ((await this.get("/openings", { wait })) as any).openings as any[];

  /** Has this been solved before? Returns the best match above `threshold`. */
  async recall(text: string, threshold = 0.4): Promise<any | null> {
    const found = (await this.get("/knowledge", { q: text })) as any;
    const best = found.matches?.[0];
    if (!best || best.score < threshold) return null;
    await fetch(`${this.platform}/knowledge/${best.entry_id}/used`,
                { method: "POST" }).catch(() => {});
    return best;
  }

  /** Who on the platform can help with this? The directory decides, not me. */
  async search(text: string): Promise<any[]> {
    const found = (await this.get("/search", { q: text, limit: 5 })) as any;
    return found.matches.filter((m: any) => m.card.did !== this.did);
  }

  /**
   * Post work for someone else — how an agent hires the guild. Leaving `to`
   * unset makes it an open call anyone qualified may claim; naming an agent
   * assigns it directly. A room is created unless you pass one.
   */
  async commission(topic: string, ask: string,
                   opts: { to?: string; roomId?: string } = {}): Promise<any> {
    const to = opts.to ?? "*";
    let roomId = opts.roomId;
    if (!roomId) {
      const room = (await this.post("/rooms", "room/create", { topic })) as any;
      roomId = room.room_id as string;
    }
    await this.join(roomId);
    const posted = (await this.say(roomId, ask, { to })) as any;
    return { roomId, utteranceId: posted.id, to };
  }

  /** Read what came back on a commission you posted. */
  async followUp(roomId: string, since = 0, wait = 0): Promise<any[]> {
    return (await this.read(roomId, since, wait)).utterances;
  }

  private async speak(roomId: string, reply: Reply): Promise<void> {
    if (!reply) return;
    const out = typeof reply === "string" ? { text: reply } : reply;
    await this.join(roomId);
    await this.say(roomId, out.text, {
      to: out.to, status: out.status, kind: out.kind,
      flagHuman: out.flagHuman, needsInput: out.needsInput,
    });
  }

  /**
   * Front-line loop: watch customer tickets and answer in them. This is the
   * Pi-facing-the-world role. `handler` sees customer messages (from:
   * "customer") and any internal reply addressed to us (from: "agent").
   */
  async serveTickets(handler: Handler): Promise<void> {
    await this.register();
    console.log(`${this.name}  did=${this.did}`);
    console.log(`watching ${this.platform} for tickets`);
    const seen = new Map<string, number>();
    for (;;) {
      try {
        for (const ticket of await this.tickets()) {
          const roomId = ticket.room_id as string;
          if (!seen.has(roomId)) {
            // Rooms outlive this process: a ticket we have not seen may be one
            // we were already serving. Resume after our own last turn rather
            // than replaying the transcript and greeting the customer twice.
            const history = await this.read(roomId);
            const ours = (history.utterances as any[])
              .filter((u) => u.author_name === this.name)
              .map((u) => u.seq as number);
            if (ours.length) {
              seen.set(roomId, Math.max(...ours));
              console.log(`resuming ${roomId} after my seq ${seen.get(roomId)}`);
            } else {
              await this.join(roomId);
              await this.say(roomId,
                `Hi ${ticket.customer}, ${this.name} here — let me take a look.`,
                { status: "open" });
              seen.set(roomId, 0);
            }
          }
          const state = await this.read(roomId, seen.get(roomId));
          for (const u of state.utterances) {
            seen.set(roomId, Math.max(seen.get(roomId) ?? 0, u.seq));
            if (u.kind === "error" && u.to === this.name) {
              await this.speak(roomId, {
                text: "I could not reach the team that owns this. I have "
                    + "passed it to a colleague and we will come back to you.",
                status: "waiting", flagHuman: true,
              });
              continue;
            }
            if (u.kind !== "say") continue;
            const external = u.author_owner === "external";
            if (!external && u.to !== this.name) continue;
            await this.speak(roomId, await handler(u.text, {
              roomId, topic: state.topic, customer: state.customer,
              askedBy: u.author_name, askedByHuman: u.author_owner,
              from: external ? "customer" : "agent", openCall: u.to === "*",
              needsInput: Boolean(u.needs_input),
            }));
          }
        }
      } catch (err) {
        console.log(`platform unreachable (${err}); retrying`);
        seen.clear();
        await new Promise((r) => setTimeout(r, 2000));
        await this.register().catch(() => {});
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
  }

  /** Internal loop: answer when mentioned, and claim open calls I can do. */
  async serve(handler: Handler): Promise<void> {
    await this.register();
    console.log(`${this.name} listening for mentions and open calls`);
    for (;;) {
      try {
        const [mine, board] = await Promise.all([
          this.mentions(this.poll).catch(() => []),
          this.openings(this.poll).catch(() => []),
        ]);
        for (const item of mine) await this.work(item, handler);
        for (const item of board) {
          if (!this.wants(item.utterance.text)) continue;
          await this.join(item.room.room_id);
          try {
            await this.claim(item.room.room_id, item.utterance.id);
          } catch {
            continue;                       // someone else got there first
          }
          await this.work(item, handler);
        }
      } catch (err) {
        console.log(`platform unreachable (${err}); retrying`);
        await new Promise((r) => setTimeout(r, 2000));
        await this.register().catch(() => {});
      }
    }
  }

  private async work(item: any, handler: Handler): Promise<void> {
    const u = item.utterance;
    await this.speak(item.room.room_id, await handler(u.text, {
      roomId: item.room.room_id, topic: item.room.topic,
      customer: item.room.customer ?? "", askedBy: u.author_name,
      askedByHuman: u.author_owner, from: "agent", openCall: u.to === "*",
    }) ?? { text: "", to: u.author_name });
  }
}

// --------------------------------------------------------------- standalone
// Replace the body of `handler` with a call into your own Pi agent loop —
// that is the only part of this file specific to what your agent does.
const argOf = (flag: string, fallback = "") => {
  const i = process.argv.indexOf(`--${flag}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
};
const flagOf = (flag: string) => process.argv.includes(`--${flag}`);

if (process.argv[1]?.endsWith("client.ts")) {
  const name = argOf("name", "Pi Agent");
  const pi = new Participant({
    name,
    owner: argOf("owner", "Kevin"),
    platform: argOf("platform", "http://127.0.0.1:9100"),
    keyDir: argOf("key-dir") || undefined,
    skills: [{
      id: argOf("skill", "support.frontline"),
      name: "Customer service",
      description: "talk to customers, triage and escalate their problems",
      tags: ["support", "customer", "service", "helpdesk"],
    }],
  });

  const handler: Handler = async (ask, ctx) => {
    if (ctx.from === "agent") {
      // An internal agent answered us: pass it on to the customer in the
      // clear (no `to`, so the platform shows it to them) and take the ticket
      // back off "waiting".
      if (ctx.needsInput) {
        return { text: `The team needs a bit more from you: ${ask}`,
                 status: "awaiting_customer" };
      }
      return {
        text: `Thanks for waiting — here is what we found: ${ask}`,
        status: "open",
      };
    }
    if (/thank|謝/i.test(ask)) {
      return { text: "Glad that helped. Closing this ticket.", status: "resolved" };
    }
    const known = await pi.recall(ask);        // ← solved before?
    if (known) {
      return {
        text: `We have seen this before — ${known.answer} `
            + `(originally worked out by ${known.by_agent} for ${known.by_human})`,
        status: "open",
      };
    }
    const helpers = await pi.search(ask);      // ← the directory decides
    if (helpers.length === 0) {
      return {
        text: `Open call — customer ${ctx.customer} asks: “${ask}”. `
            + "Whoever owns this, please take it.",
        to: "*", status: "waiting",
      };
    }
    const who = helpers[0].card.name as string;
    return {
      text: `@${who} customer ${ctx.customer} says: “${ask}”. Can you take a look?`,
      to: who, status: "waiting",
    };
  };

  if (flagOf("internal")) await pi.serve(handler);
  else await pi.serveTickets(handler);
}
