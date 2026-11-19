const H = { 'X-Requested-With': 'penish' };

// The server's detail is already in the site's words (`book vi, leaf 9 is taken`), so it
// is what the page shows.
export class CollectionError extends Error {}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
	const r = await fetch(`/api/admin/collections${path}`, {
		method,
		credentials: 'same-origin',
		headers: body === undefined ? H : { ...H, 'Content-Type': 'application/json' },
		body: body === undefined ? undefined : JSON.stringify(body)
	});
	if (!r.ok) {
		let detail = 'failed';
		try {
			const d = (await r.json()).detail;
			if (typeof d === 'string') detail = d;
		} catch {
			// Not json; the generic word will do.
		}
		throw new CollectionError(detail);
	}
	const text = await r.text();
	return (text ? JSON.parse(text) : null) as T;
}

export type Pos = { book: number; position: number };
export type Pic = { image_id: number; slug: string | null; status: string | null; thumb: string | null };
export type AdminLeaf = Pic & { position: number; pinned_book: boolean; pinned_position: boolean };
export type AdminBook = {
	number: number;
	title: string | null;
	anchor_text: string;
	tag_hints: string[];
	anchored: boolean;
	leaves: AdminLeaf[];
};
export type Request = Pic & {
	book: number | null;
	position: number | null;
	error: string | null;
	created_at: string;
};
export type HistoryRow = Pic & { from: Pos | null; to: Pos | null; reason: string; at: string };
export type Move = Pic & { from: Pos; to: Pos; reason: string };
export type AdminCollection = {
	slug: string;
	title: string;
	abbrev: string;
	abbrev_latin: string;
	preamble: string;
	published: boolean;
	book_count: number;
	leaves_per_book: number;
	resorted_at: string | null;
	books: AdminBook[];
	requests: Request[];
	history: HistoryRow[];
};
export type Tuning = Record<string, { value: number | boolean; default: number | boolean }>;

export const list = () => call<{ collections: AdminCollection[]; tuning: Tuning }>('GET', '');

export const patch = (slug: string, body: Partial<Pick<AdminCollection, 'title' | 'preamble' | 'published'>>) =>
	call<AdminCollection>('PATCH', `/${slug}`, body);

export const patchBook = (
	slug: string,
	n: number,
	body: { title?: string; anchor_text?: string; tag_hints?: string[] }
) => call<AdminCollection>('PATCH', `/${slug}/books/${n}`, body);

// Book and leaf go as typed: the server reads roman or arabic.
export const place = (slug: string, imageId: number, book: string, position: string) =>
	call<{ state: string; moves: Move[] }>('PUT', `/${slug}/pictures/${imageId}`, {
		book: book.trim() || null,
		position: position.trim() || null
	});
export const unfile = (slug: string, imageId: number) => call('DELETE', `/${slug}/pictures/${imageId}`);
export const pin = (slug: string, imageId: number, pinned: boolean) =>
	call('POST', `/${slug}/pictures/${imageId}/pin`, { pinned });
export const retry = (slug: string, imageId: number) =>
	call<{ queued: boolean }>('POST', `/${slug}/requests/${imageId}/retry`);
export const resort = (slug: string, dryRun: boolean) =>
	call<{ dry_run: boolean; moves: Move[] }>('POST', `/${slug}/resort`, { dry_run: dryRun });

export type PictureCollection = {
	slug: string;
	title: string;
	abbrev_latin: string;
	book_count: number;
	leaves_per_book: number;
	leaf: { book: number; position: number; pinned_book: boolean; pinned_position: boolean } | null;
	request: { book: number | null; position: number | null; error: string | null } | null;
};
export const forPicture = (imageId: number) =>
	call<{ collections: PictureCollection[] }>('GET', `/picture/${imageId}`);

// The tunables live with the models, under the same settings table.
export async function tune(key: string, value: number | boolean): Promise<void> {
	const r = await fetch('/api/admin/models', {
		method: 'PUT',
		credentials: 'same-origin',
		headers: { ...H, 'Content-Type': 'application/json' },
		body: JSON.stringify({ key, value })
	});
	if (!r.ok) {
		const d = await r.json().catch(() => ({}));
		throw new CollectionError(typeof d.detail === 'string' ? d.detail : 'failed');
	}
}
