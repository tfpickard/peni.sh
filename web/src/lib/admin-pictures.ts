import type { Variant } from './api';

export type Item = {
	id: number;
	slug: string | null;
	status: string;
	thumb: string | null;
	caption: string | null;
	net: number;
	created_at: string;
};
export type Page = { items: Item[]; next_cursor: string | null };
export type Field = 'caption' | 'description' | 'analysis' | 'prompt' | 'tags' | 'slug';
export type Regen = 'caption' | 'description' | 'analysis' | 'tags' | 'haiku' | 'embedding';
export type Text = { body: string; source: string; model: string | null };
export type Rect = { x: number; y: number; w: number; h: number };
export type Job = {
	id: number | string;
	type: string;
	status: string;
	last_error?: string | null;
	created_at?: string;
};
// The spec leaves the detail shape open; this is the shape the page reads and
// `detail()` fills defaults for anything the server omits.
export type Detail = {
	id: number;
	slug: string;
	status: string;
	width: number;
	height: number;
	variants: Variant[];
	caption: Text | null;
	description: Text | null;
	analysis: Text | null;
	prompt: Text | null;
	slug_source: string | null;
	tags: { tag: string; source: string; model?: string | null }[];
	crop: Rect | null;
	votes: { up: number; down: number; net: number };
	comments: { pending: number; approved: number; rejected: number };
	jobs: Job[];
};

const H = { 'X-Requested-With': 'penish' };

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
	const headers: Record<string, string> = { ...H };
	if (init.body) headers['Content-Type'] = 'application/json';
	const r = await fetch(`/api/admin/pictures${path}`, {
		...init,
		headers,
		credentials: 'same-origin'
	});
	if (!r.ok) throw new HttpError(r.status);
	const type = r.headers.get('content-type') ?? '';
	return (type.includes('json') ? r.json() : undefined) as T;
}

export class HttpError extends Error {
	status: number;
	constructor(status: number) {
		super(String(status));
		this.status = status;
	}
}

export type Query = { q: string; status: string; sort: string; cursor: string | null };

export function list(p: Query): Promise<Page> {
	const q = new URLSearchParams();
	if (p.q) q.set('q', p.q);
	if (p.status) q.set('status', p.status);
	if (p.sort) q.set('sort', p.sort);
	if (p.cursor) q.set('cursor', p.cursor);
	return call<Page>(`?${q}`);
}

export async function detail(id: string | number): Promise<Detail> {
	const d = await call<Partial<Detail>>(`/${id}`);
	return {
		id: Number(id),
		slug: '',
		status: '',
		width: 0,
		height: 0,
		variants: [],
		caption: null,
		description: null,
		analysis: null,
		prompt: null,
		slug_source: null,
		tags: [],
		crop: null,
		votes: { up: 0, down: 0, net: 0 },
		comments: { pending: 0, approved: 0, rejected: 0 },
		jobs: [],
		...d
	};
}

export const patch = (id: number, body: Partial<Record<Field, string | string[]>>) =>
	call<unknown>(`/${id}`, { method: 'PATCH', body: JSON.stringify(body) });

export const regen = (id: number, fields: Regen[]) =>
	call<unknown>(`/${id}/regen`, { method: 'POST', body: JSON.stringify({ fields }) });

export const crop = (id: number, rect: Rect | null) =>
	call<unknown>(`/${id}/crop`, { method: 'POST', body: JSON.stringify({ rect }) });

export const remove = (id: number) => call<unknown>(`/${id}`, { method: 'DELETE' });

export const originalUrl = (id: number) => `/api/admin/pictures/${id}/original`;

export const active = (jobs: Job[]) => jobs.some((j) => j.status === 'queued' || j.status === 'running');
