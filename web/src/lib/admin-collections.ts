// Mirrors penish/api/admin_collections.py. Every write answers with the whole collection.
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
	const r = await fetch(`/api/admin/collections${path}`, {
		method,
		credentials: 'same-origin',
		headers: body === undefined ? H : { ...H, 'Content-Type': 'application/json' },
		body: body === undefined ? undefined : JSON.stringify(body)
	});
	if (!r.ok) {
		// A refused move says why (`book iii leaf 14 is taken`); the page shows it as is.
		let detail: string | null = null;
		try {
			const j = await r.json();
			if (typeof j?.detail === 'string') detail = j.detail;
		} catch {
			detail = null;
		}
		throw new HttpError(r.status, detail);
	}
	return r.json();
}

export type Summary = {
	slug: string;
	title: string;
	abbrev: string;
	abbrev_latin: string;
	book_count: number;
	leaves_per_book: number;
	published: boolean;
};
export type Tuning = {
	anchor_k: number;
	tag_alpha: number;
	fill_beta: number;
	book_margin: number;
	weekly_resort: boolean;
};
export type AdminLeaf = {
	image_id: number;
	slug: string | null;
	status: string;
	thumb: string | null;
	position: number;
	pinned: boolean;
	book_pinned: boolean;
	note: string | null;
	note_source: 'owner' | 'model' | null;
};
export type AdminBook = {
	number: number;
	title: string | null;
	anchor_text: string;
	tag_hints: string[];
	// Embedded under today's model; false while the worker has it queued.
	anchored: boolean;
	leaves: AdminLeaf[];
};
export type FilingRequest = {
	image_id: number;
	slug: string | null;
	status: string;
	thumb: string | null;
	book: number | null;
	position: number | null;
	error: string | null;
	job: string | null;
	created_at: string;
};
export type Upload = {
	stem: string;
	sha256: string;
	filename: string | null;
	stage: 'received' | 'inbox' | 'ingesting' | 'failed' | 'down' | 'live' | 'unfiled' | 'lost';
	error: string | null;
	slug: string | null;
	image_id: number | null;
	created_at: string;
};
export type HistoryRow = {
	id: number;
	image_id: number;
	slug: string | null;
	from_book: number | null;
	from_position: number | null;
	to_book: number | null;
	to_position: number | null;
	reason: 'auto' | 'resort' | 'owner';
	at: string;
};
export type Detail = Summary & {
	id: number;
	preamble: string;
	gloss: string;
	record: [string, string][];
	vendor_text: string | null;
	// Conservator notes waiting or being written for this collection.
	notes_queued: number;
	tuning: Tuning;
	tuning_default: Tuning;
	last_resort_at: string | null;
	resort: { status: string; last_error: string | null } | null;
	books: AdminBook[];
	requests: FilingRequest[];
	// Uploads for this collection that are not leaves yet; the server omits any already in `requests`.
	uploads: Upload[];
	history: HistoryRow[];
};
export type ProposedMove = {
	image_id: number;
	slug: string | null;
	before: [number, number] | null;
	after: [number, number] | null;
};

const at = (slug: string) => `/${encodeURIComponent(slug)}`;

export const list = () => call<{ collections: Summary[] }>('GET', '');
export const detail = (slug: string) => call<Detail>('GET', at(slug));
export const edit = (
	slug: string,
	body: Partial<
		Pick<
			Detail,
			'title' | 'abbrev' | 'abbrev_latin' | 'preamble' | 'gloss' | 'record' | 'vendor_text' | 'published'
		>
	> & {
		tuning?: Partial<Tuning>;
	}
) => call<Detail>('PATCH', at(slug), body);
export const editBook = (
	slug: string,
	n: number,
	body: { title?: string; anchor_text?: string; tag_hints?: string[] }
) => call<Detail>('PATCH', `${at(slug)}/books/${n}`, body);
// Filed: a move, pinned. Unfiled: a request the filer takes from there.
export const place = (slug: string, imageId: number, book: number | null, position: number | null) =>
	call<Detail>('PUT', `${at(slug)}/leaves/${imageId}`, { book, position });
export const unfile = (slug: string, imageId: number) =>
	call<Detail>('DELETE', `${at(slug)}/leaves/${imageId}`);
export const pin = (slug: string, imageId: number, pinned: boolean) =>
	call<Detail>('POST', `${at(slug)}/leaves/${imageId}/pin`, { pinned });
// The owner's note; empty clears it and hands the leaf back to the conservator.
export const setNote = (slug: string, imageId: number, note: string) =>
	call<Detail>('PUT', `${at(slug)}/leaves/${imageId}/note`, { note });
// Ask the conservator for one leaf (a model note is written again; an owner note is refused).
export const writeNote = (slug: string, imageId: number) =>
	call<Detail>('POST', `${at(slug)}/leaves/${imageId}/note`, {});
export const writeMissingNotes = (slug: string) => call<Detail>('POST', `${at(slug)}/notes`, {});
export const retry = (slug: string, imageId: number) =>
	call<Detail>('POST', `${at(slug)}/requests/${imageId}/retry`, {});
export const dryRun = (slug: string) =>
	call<{ moves: ProposedMove[] }>('POST', `${at(slug)}/resort`, { dry_run: true });
export const resort = (slug: string) => call<Detail>('POST', `${at(slug)}/resort`, { dry_run: false });
