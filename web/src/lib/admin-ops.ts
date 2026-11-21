const H = { 'X-Requested-With': 'penish' };

export class HttpError extends Error {
	constructor(
		public status: number,
		public detail: string | null
	) {
		super(detail ?? String(status));
	}
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
	const r = await fetch(`/api/admin${path}`, {
		method,
		credentials: 'same-origin',
		headers: body === undefined ? H : { ...H, 'Content-Type': 'application/json' },
		body: body === undefined ? undefined : JSON.stringify(body)
	});
	if (!r.ok) {
		// A refused setting says why (`claude-x is not another provider`); pages may show it.
		let detail: string | null = null;
		try {
			const j = await r.json();
			if (typeof j?.detail === 'string') detail = j.detail;
		} catch {
			detail = null;
		}
		throw new HttpError(r.status, detail);
	}
	const text = await r.text();
	return (text ? JSON.parse(text) : null) as T;
}

const get = <T>(p: string) => call<T>('GET', p);
const post = <T = unknown>(p: string, b?: unknown) => call<T>('POST', p, b ?? {});
const del = (p: string) => call<unknown>('DELETE', p);

export type CommentStatus = 'pending' | 'approved' | 'rejected';
export type Comment = {
	id: number;
	body: string;
	name: string | null;
	created_at: string;
	slug: string;
	thumb: string | null;
	status: CommentStatus;
};
export const comments = (status: CommentStatus) => get<Comment[]>(`/comments?status=${status}`);
export const setComment = (id: number, status: CommentStatus) => post(`/comments/${id}`, { status });
export const deleteComment = (id: number) => del(`/comments/${id}`);

// gallery is the serial of the gallery the haiku was written for; null for the deck.
// model is the model that wrote it; null for owner and seed haikus and older model ones.
export type Haiku = {
	id: number;
	lines: string[];
	source: string;
	model: string | null;
	active: boolean;
	gallery: number | null;
};
export type HaikuPage = { items: Haiku[]; total: number };
export const haikus = (source: string, active: string, limit = 50, offset = 0) => {
	const q = new URLSearchParams({ limit: String(limit), offset: String(offset) });
	if (source) q.set('source', source);
	if (active) q.set('active', active);
	return get<HaikuPage>(`/haikus?${q}`);
};
export const addHaiku = (lines: string[]) => post('/haikus', { lines });
export const patchHaiku = (id: number, p: { lines?: string[]; active?: boolean }) =>
	call('PATCH', `/haikus/${id}`, p);
export const deleteHaiku = (id: number) => del(`/haikus/${id}`);
export const growHaikus = (n: number) => post('/haikus/grow', { n });

export type Models = {
	// Strings for models and prompts, null for an unset effort, a number for mycelium.daily_calls,
	// a list of `provider:model` ids for haiku.fallback, a boolean for filer.judge.
	current: Record<string, string | number | boolean | string[] | null>;
	default: Record<string, string | number | boolean | string[] | null>;
	efforts: string[];
	known_models: string[];
	purposes: string[];
	providers: string[];
	// Providers with a key in the worker's env; null until the worker reports them.
	keyed: string[] | null;
	calls_today: number;
};
export const models = () => get<Models>('/models');
export const putSetting = (key: string, value: unknown) => call('PUT', '/models', { key, value });
export const resetSetting = (key: string) => del(`/models/${key}`);
export const reembedAll = () => post('/models/reembed_all');
export const redescribeAll = () => post('/models/redescribe_all');

export type Job = {
	id: number;
	type: string;
	status: string;
	attempts: number;
	run_after: string | null;
	last_error: string | null;
};
export type FailedDir = { name: string; files: string[]; err: string; at: string };
export type Queue = { jobs: Job[]; failed: FailedDir[] };
export const queue = () => get<Queue>('/queue');
export const retryJob = (id: number) => post(`/queue/${id}/retry`);
export const deleteJob = (id: number) => del(`/queue/${id}`);
export const retryFailed = (name: string) => post(`/queue/failed/${encodeURIComponent(name)}/retry`);
export const deleteFailed = (name: string) => del(`/queue/failed/${encodeURIComponent(name)}`);

export type Ranked = { id: number; slug: string; thumb: string | null; net: number };
export type Viewed = { id: number; slug: string; thumb: string | null; n: number; days: number[] };
export type Stats = {
	viewed: { last_7d: Viewed[]; last_30d: Viewed[] };
	referrers: { last_7d: { host: string; n: number }[]; last_30d: { host: string; n: number }[] };
	pictures: number;
	by_status: Record<string, number>;
	votes_total: number;
	comments_pending: number;
	top: Ranked[];
	bottom: Ranked[];
	temperature_history: { model: string; n: number; temperature: number; at: string }[];
	api_usage: { last_30d: { day: string; model: string; in: number; out: number }[] };
};
export const stats = () => get<Stats>('/stats');

export type RestoreDrill = {
	ok: boolean;
	ran_at: string | null;
	dump: string | null;
	reason: string | null;
	counts: Record<string, number>;
	local_only: boolean | null;
};

export type System = {
	deployed_sha: string | null;
	worker_heartbeat_at: string | null;
	worker_info: unknown;
	disk: { total: number; used: number; free: number };
	last_backup_at: string | null;
	restore_drill: RestoreDrill | null;
	backup_local_only: boolean | null;
	db_size: number;
	recent_errors: { id: number; type: string; status: string; attempts: number; error: string; at: string }[];
};
export const system = () => get<System>('/system');
