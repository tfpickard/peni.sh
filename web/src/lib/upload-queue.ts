import * as admin from './admin';

// The server's cap, checked here so a file that cannot go is refused without sending it.
export const MAX_BYTES = 50 * 1024 * 1024;
const CONCURRENCY = 3;
const ATTEMPTS = 3;
// 2 s after the first counted failure, 4 s after the second.
const BACKOFF = 2000;
// A send that makes no progress this long while the page is in view is dead. Once the
// body is out the server is still writing and syncing it, so that wait is longer.
const STALL = 30_000;
const STALL_SENT = 120_000;
const DOG = 5000;
// After a lost answer with the whole body out, the file can sit in the inbox or behind
// other ingest jobs, where status cannot see it yet. Past this it is sent again: ingest is
// keyed by sha256, so a second copy is the same picture processed twice, not two pictures.
const GRACE = 60_000;
const GRACE_STEP = 5000;
const POLL_FIRST = 2000;
const POLL_MAX = 20_000;
// Counted in visible time only. A picture not settled by then belongs to the queue page.
export const WATCH_LIMIT = 30 * 60_000;
export const SESSION_ENDED = 'session ended';

export type Phase = 'new' | 'queued' | 'hashing' | 'checking' | 'uploading' | 'sent' | 'failed';

// Mutated in place, so a reactive proxy handed in renders every change.
export type Item = {
	name: string;
	file: File;
	// Taken when the item is posted, so a retry sends what was asked, whatever the form
	// says by then.
	fields: admin.Fields | null;
	filing: admin.Filing | null;
	phase: Phase;
	progress: number;
	// The server's word once sent: waiting, processing, pending_enrichment, live, failed.
	state: string;
	slug?: string;
	error?: string;
	// Polling has stopped and the last state stands.
	quiet: boolean;
	sha256: string;
	stem: string;
};

export type Hooks = {
	// Nothing is left to send; sent pictures may still be watched.
	ondrain?: () => void;
	// Every posted picture is settled, failed, or no longer watched.
	ondone?: () => void;
	onhalt?: (halted: boolean) => void;
};

type Run = { attempts: number; tried: boolean; bodySent: boolean; watched: number };

export function item(file: File): Item {
	return {
		name: file.name,
		file,
		fields: null,
		filing: null,
		phase: 'new',
		progress: 0,
		state: '',
		quiet: false,
		sha256: '',
		stem: ''
	};
}

const SENDING: readonly Phase[] = ['queued', 'hashing', 'checking', 'uploading'];
export const isSending = (i: Item) => SENDING.includes(i.phase);
export const isFailed = (i: Item) =>
	i.phase === 'failed' || (i.phase === 'sent' && i.state === 'failed');

export function summary(items: readonly Item[]): string {
	const posted = items.filter((i) => i.phase !== 'new');
	if (!posted.length) return '';
	const sent = posted.filter((i) => i.phase === 'sent');
	const live = sent.filter((i) => i.state === 'live').length;
	const waiting = sent.filter((i) => i.state !== 'live' && i.state !== 'failed').length;
	const failed = posted.filter(isFailed).length;
	const parts = [`${sent.length} of ${posted.length} sent`];
	if (live) parts.push(`${live} live`);
	if (waiting) parts.push(`${waiting} waiting`);
	if (failed) parts.push(`${failed} failed`);
	return parts.join(', ');
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
const settled = (state: string) => state === 'live' || state === 'failed';
const gone = (e: unknown) => e instanceof admin.HttpError && (e.status === 401 || e.status === 404);

async function digest(file: File): Promise<string> {
	// Absent outside a secure context, such as plain http to a LAN address in dev. Without
	// a hash a retry cannot ask first and simply sends again.
	const subtle = globalThis.crypto?.subtle;
	if (!subtle) return '';
	try {
		const buf = await subtle.digest('SHA-256', await file.arrayBuffer());
		return Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, '0')).join('');
	} catch {
		return '';
	}
}

export class UploadQueue {
	private items: Item[] = [];
	private runs = new Map<Item, Run>();
	// Items holding one of the send slots, including ones parked until the page is back.
	private sending = new Set<Item>();
	private tracking = new Set<Item>();
	private visible = true;
	private online = true;
	// Bumped on every hide and every offline, so a send can tell it lived through one: a
	// request the browser killed then is a lost answer, not a spent attempt.
	private epoch = 0;
	private waiters: (() => void)[] = [];
	private halted = false;
	private busy = false;
	private owed = false;
	private hooks: Hooks;

	constructor(hooks: Hooks = {}) {
		this.hooks = hooks;
	}

	add(items: readonly Item[]): void {
		this.unhalt();
		for (const i of items) {
			if (this.runs.has(i)) continue;
			this.items.push(i);
			this.runs.set(i, { attempts: 0, tried: false, bodySent: false, watched: 0 });
			i.phase = 'queued';
			this.owed = true;
		}
		this.pump();
	}

	retryFailed(): void {
		this.unhalt();
		for (const i of this.items) {
			if (i.phase !== 'failed') continue;
			this.runs.get(i)!.attempts = 0;
			i.phase = 'queued';
			i.error = undefined;
			i.progress = 0;
			this.owed = true;
		}
		this.pump();
	}

	// Anything still sending or watched.
	get active(): boolean {
		return this.sending.size > 0 || this.tracking.size > 0 || this.items.some(isSending);
	}

	// Forgets a finished batch. Refused while anything is still moving.
	clear(): boolean {
		if (this.active) return false;
		this.items = [];
		this.runs.clear();
		return true;
	}

	setVisible(v: boolean): void {
		if (!v && this.visible) this.epoch++;
		this.visible = v;
		this.wake();
	}

	setOnline(v: boolean): void {
		if (!v && this.online) this.epoch++;
		this.online = v;
		this.wake();
	}

	private get awake(): boolean {
		return this.visible && this.online;
	}

	private wake(): void {
		if (!this.awake) return;
		const w = this.waiters;
		this.waiters = [];
		for (const f of w) f();
	}

	private untilAwake(): Promise<void> {
		return this.awake ? Promise.resolve() : new Promise((r) => this.waiters.push(r));
	}

	private pump(): void {
		if (!this.halted) {
			for (const i of this.items) {
				if (this.sending.size >= CONCURRENCY) break;
				if (i.phase !== 'queued' || this.sending.has(i)) continue;
				this.sending.add(i);
				void this.send(i).finally(() => {
					this.sending.delete(i);
					this.pump();
				});
			}
		}
		this.settle();
	}

	private settle(): void {
		const busy = this.items.some(isSending);
		if (this.busy && !busy) this.hooks.ondrain?.();
		this.busy = busy;
		if (this.owed && !this.active) {
			this.owed = false;
			this.hooks.ondone?.();
		}
	}

	private halt(): void {
		if (this.halted) return;
		this.halted = true;
		for (const i of this.items) {
			if (i.phase === 'queued' && !this.sending.has(i)) this.fail(i, SESSION_ENDED);
		}
		this.hooks.onhalt?.(true);
	}

	private unhalt(): void {
		if (!this.halted) return;
		this.halted = false;
		this.hooks.onhalt?.(false);
		for (const i of this.items) {
			if (i.phase === 'sent' && i.quiet && this.runs.get(i)!.watched < WATCH_LIMIT) {
				this.owed = true;
				void this.track(i);
			}
		}
	}

	private fail(i: Item, why: string): void {
		i.phase = 'failed';
		i.state = '';
		i.error = why;
	}

	private accept(i: Item, s: admin.AdminState): void {
		i.phase = 'sent';
		i.progress = 1;
		i.state = s.state;
		i.slug = s.slug;
		i.error = s.error;
		void this.track(i);
	}

	private async send(i: Item): Promise<void> {
		const run = this.runs.get(i)!;
		try {
			if (i.file.size > MAX_BYTES) return this.fail(i, 'too large');
			if (!i.sha256) {
				i.phase = 'hashing';
				i.sha256 = await digest(i.file);
			}
			while (true) {
				if (this.halted) return this.fail(i, SESSION_ENDED);
				if (run.tried) {
					const known = await this.known(i, run);
					if (this.halted) return this.fail(i, SESSION_ENDED);
					if (known) return this.accept(i, known);
				}
				if (!this.online) {
					i.phase = 'queued';
					await this.untilAwake();
					continue;
				}
				const epoch = this.epoch;
				const r = await this.attempt(i, run);
				if ('stem' in r) {
					i.sha256 = r.sha256;
					i.stem = r.stem;
					return this.accept(i, { state: 'waiting' });
				}
				if (gone(r)) {
					this.halt();
					return this.fail(i, SESSION_ENDED);
				}
				if (r.status === 413 || r.status === 415 || r.status === 422) {
					return this.fail(i, r.message);
				}
				const lost = r.status === 0 && (this.epoch !== epoch || !this.awake);
				i.phase = 'queued';
				i.progress = 0;
				if (lost) {
					await this.untilAwake();
					continue;
				}
				if (++run.attempts >= ATTEMPTS) return this.fail(i, r.message);
				await sleep(BACKOFF * 2 ** (run.attempts - 1));
			}
		} catch {
			this.fail(i, 'failed');
		}
	}

	private async attempt(
		i: Item,
		run: Run
	): Promise<{ stem: string; sha256: string } | admin.HttpError> {
		i.phase = 'uploading';
		i.progress = 0;
		run.tried = true;
		const ctl = new AbortController();
		let last = Date.now();
		let seen = this.epoch;
		let out = false;
		const dog = setInterval(() => {
			// Hidden time is not stall time, and a page just back gets a fresh window.
			if (!this.awake || this.epoch !== seen) {
				seen = this.epoch;
				last = Date.now();
				return;
			}
			if (Date.now() - last > (out ? STALL_SENT : STALL)) ctl.abort();
		}, DOG);
		const progress = (p: number) => {
			i.progress = p;
			last = Date.now();
			if (p >= 1) out = true;
		};
		try {
			return await admin.upload(i.file, i.fields, progress, i.filing, ctl.signal);
		} catch (e) {
			const err = e instanceof admin.HttpError ? e : new admin.HttpError('no answer', 0, out);
			if (err.sent || out) run.bodySent = true;
			return err;
		} finally {
			clearInterval(dog);
		}
	}

	// Whether the server already holds the picture, asked by sha256 alone because a lost
	// answer also lost the stem. null means send it.
	private async known(i: Item, run: Run): Promise<admin.AdminState | null> {
		if (!i.sha256) return null;
		i.phase = 'checking';
		let waited = 0;
		while (true) {
			try {
				const s = await admin.status(i.sha256, i.stem);
				if (s.state !== 'waiting') return s;
			} catch (e) {
				if (gone(e)) this.halt();
				return null;
			}
			// waiting is all status can say about a file that never arrived, and also about
			// one still settling in the inbox. Only a body that fully left can be the second.
			if (!run.bodySent || waited >= GRACE) return null;
			const epoch = this.epoch;
			await sleep(GRACE_STEP);
			await this.untilAwake();
			if (this.halted) return null;
			if (this.epoch === epoch) waited += GRACE_STEP;
		}
	}

	private async track(i: Item): Promise<void> {
		if (this.tracking.has(i)) return;
		const run = this.runs.get(i)!;
		this.tracking.add(i);
		i.quiet = false;
		let delay = POLL_FIRST;
		try {
			while (!settled(i.state)) {
				if (this.halted || run.watched >= WATCH_LIMIT) {
					i.quiet = true;
					return;
				}
				const epoch = this.epoch;
				await sleep(delay);
				await this.untilAwake();
				if (this.epoch === epoch) run.watched += delay;
				if (this.halted) continue;
				try {
					const s = await admin.status(i.sha256, i.stem);
					i.state = s.state;
					i.slug = s.slug;
					i.error = s.error;
				} catch (e) {
					// A dropped poll is retried; the next one tells the truth.
					if (gone(e)) this.halt();
				}
				delay = Math.min(delay * 1.5, POLL_MAX);
			}
		} finally {
			this.tracking.delete(i);
			this.settle();
		}
	}
}
