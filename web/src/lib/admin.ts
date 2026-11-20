// stem comes with `received`: the server holds the bytes under that name, no picture yet.
export type AdminState = { state: string; slug?: string; error?: string; stem?: string };
export type Recent = { slug: string | null; status: string; created_at: string; thumb: string | null };
export type Fields = { caption: string; description: string; tags: string; prompt: string };
// Written as a `_collection.txt` sidecar by the server. book and leaf blank: the filer picks.
// note, if any, becomes `_note.txt`: the owner's conservator note for the leaf.
export type Filing = { collection: string; book: string; leaf: string; note: string };

const H = { 'X-Requested-With': 'penish' };

export async function me(): Promise<boolean> {
	const r = await fetch('/api/admin/me');
	return r.ok;
}

// 'no' and 'wait' are the words the form shows, so the page does not interpret status codes.
export async function login(password: string): Promise<'ok' | 'no' | 'wait'> {
	const r = await fetch('/api/admin/login', {
		method: 'POST',
		headers: { ...H, 'Content-Type': 'application/json' },
		body: JSON.stringify({ password })
	});
	if (r.ok) return 'ok';
	return r.status === 429 ? 'wait' : 'no';
}

export const logout = () => fetch('/api/admin/logout', { method: 'POST', headers: H });

// status 0 means no answer at all: offline, aborted, or the connection dropped. sent says
// the whole body left and no usable answer came back, since only then can the server hold
// the file without having said so. A refusal with a status code stored nothing.
export class HttpError extends Error {
	status: number;
	sent: boolean;
	constructor(message: string, status: number, sent = false) {
		super(message);
		this.status = status;
		this.sent = sent;
	}
}

// The server's own word for a refusal, when it gave one: 422 names the bad field.
function detail(text: string): string | null {
	try {
		const d = JSON.parse(text)?.detail;
		return typeof d === 'string' && d ? d : null;
	} catch {
		return null;
	}
}

function reason(status: number, text: string): string {
	if (status === 413) return 'too large';
	if (status === 415) return 'not an image';
	if (status === 422) return detail(text) ?? 'refused';
	if (status === 0) return 'no answer';
	return `server ${status}`;
}

// A gateway that gave up waiting on the api: the api may still have kept the file.
const unanswered = (status: number) =>
	status === 502 || status === 504 || (status >= 520 && status <= 524);

// A 200 from something other than the api, such as a captive portal, is not a receipt.
function receipt(text: string): { stem: string; sha256: string } | null {
	try {
		const r = JSON.parse(text);
		return typeof r?.stem === 'string' && typeof r?.sha256 === 'string'
			? { stem: r.stem, sha256: r.sha256 }
			: null;
	} catch {
		return null;
	}
}

// XHR rather than fetch: fetch has no upload progress. Unbounded here; the caller's signal
// is its bound.
export function upload(
	file: File,
	fields: Fields | null,
	onProgress: (fraction: number) => void,
	filing: Filing | null = null,
	signal?: AbortSignal
): Promise<{ stem: string; sha256: string }> {
	return new Promise((resolve, reject) => {
		const body = new FormData();
		body.append('image', file);
		if (fields) for (const [k, v] of Object.entries(fields)) if (v) body.append(k, v);
		if (filing?.collection) for (const [k, v] of Object.entries(filing)) if (v) body.append(k, v);
		const xhr = new XMLHttpRequest();
		let sent = false;
		const lost = () => reject(new HttpError('no answer', 0, sent));
		xhr.open('POST', '/api/admin/upload');
		xhr.setRequestHeader('X-Requested-With', 'penish');
		xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
		xhr.upload.onload = () => (sent = true);
		// Settles on every path: a throw in here would leave the promise open, and aborting
		// a request that is already done fires nothing.
		xhr.onload = () => {
			if (xhr.status === 200) {
				const r = receipt(xhr.responseText);
				if (r) resolve(r);
				else reject(new HttpError('bad answer', 200, sent));
			} else {
				const s = xhr.status;
				reject(new HttpError(reason(s, xhr.responseText), s, sent && (s === 0 || unanswered(s))));
			}
		};
		xhr.onerror = lost;
		xhr.onabort = lost;
		if (signal?.aborted) return lost();
		signal?.addEventListener('abort', () => xhr.abort(), { once: true });
		xhr.send(body);
	});
}

// Polled, so a hung request must not hold its caller.
const STATUS_LIMIT = 15_000;

function asState(v: unknown): AdminState | null {
	if (typeof v !== 'object' || v === null) return null;
	const o = v as Record<string, unknown>;
	if (typeof o.state !== 'string') return null;
	const s: AdminState = { state: o.state };
	if (typeof o.slug === 'string') s.slug = o.slug;
	if (typeof o.error === 'string') s.error = o.error;
	if (typeof o.stem === 'string') s.stem = o.stem;
	return s;
}

export async function status(sha256: string, stem: string, signal?: AbortSignal): Promise<AdminState> {
	const q = new URLSearchParams({ sha256, stem });
	const ctl = new AbortController();
	const stop = () => ctl.abort();
	const t = setTimeout(stop, STATUS_LIMIT);
	if (signal?.aborted) stop();
	signal?.addEventListener('abort', stop, { once: true });
	try {
		let r: Response;
		try {
			r = await fetch(`/api/admin/status?${q}`, { signal: ctl.signal });
		} catch {
			throw new HttpError('no answer', 0);
		}
		if (!r.ok) throw new HttpError('status', r.status);
		let s: AdminState | null = null;
		try {
			s = asState(await r.json());
		} catch {
			if (ctl.signal.aborted) throw new HttpError('no answer', 0);
		}
		if (!s) throw new HttpError('bad answer', r.status);
		return s;
	} finally {
		clearTimeout(t);
		signal?.removeEventListener('abort', stop);
	}
}

export async function recent(): Promise<Recent[]> {
	const r = await fetch('/api/admin/recent');
	if (!r.ok) throw new Error('recent');
	const v: unknown = await r.json();
	if (!Array.isArray(v)) throw new Error('recent');
	return v;
}
