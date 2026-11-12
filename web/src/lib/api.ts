export type Haiku = { id: number | string; lines: string[]; seed?: number; gen?: string };
export type Stats = { pictures: number; temperature: number };

async function get<T>(path: string): Promise<T> {
	const res = await fetch(path);
	if (!res.ok) throw new Error(`${path} ${res.status}`);
	return res.json();
}

export const getHaiku = () => get<Haiku>('/api/haiku');
export const getStats = () => get<Stats>('/api/stats');

export type Variant = { w: number; h: number; avif: string | null; webp: string | null };
export type ImageItem = {
	slug: string;
	width: number;
	height: number;
	blurhash: string;
	palette: string[];
	variants: Variant[];
};
// count rides only on the first page of a tag listing.
export type ImagePage = { items: ImageItem[]; next_cursor: string | null; count?: number };
export type TagCount = { tag: string; n: number };
export type SearchResult = { q: string; tags: TagCount[]; items: ImageItem[] };
export type Text = { body: string; source: string; model: string | null };
export type Neighbor = { slug: string; score: number; blurhash: string; src: string };
export type Votes = { up: number; down: number; net: number; mine: -1 | 0 | 1 };
export type Comment = { name: string | null; body: string; created_at: string };
export type ImageDetail = ImageItem & {
	caption: Text | null;
	description: Text | null;
	prompt: Text | null;
	tags: { tag: string; source: string }[];
	categories: { slug: string; label: string }[];
	similar: Neighbor[];
	dissimilar: Neighbor[];
	mode: 'raw' | 'centered';
	metrics: { loneliness: number | null; typicality: number | null; surprise: number | null };
	published_at: string;
	votes: Votes;
	comments: Comment[];
	// Neighbours in grid order, wrapping; null only when this is the sole picture.
	prev: string | null;
	next: string | null;
};
export type ImageRedirect = { redirect: string };

// Mirrors penish/sort.py. The labels are what the picker shows.
export const SORTS = [
	['newest', 'newest'],
	['oldest', 'oldest'],
	['random', 'random'],
	['drifting', 'drifting'],
	['oldest-drifting', 'oldest, drifting'],
	['clumped', 'clumped'],
	['anti-clumped', 'anti-clumped'],
	['rainbow', 'rainbow'],
	['time-of-day', 'time of day'],
	['lonely', 'lonely'],
	['surprising', 'surprising'],
	['drunkards-walk', "drunkard's walk"],
	['tidal', 'tidal']
] as const;
export type Sort = (typeof SORTS)[number][0];
export const SEEDED: readonly Sort[] = ['random', 'drunkards-walk'];
export const isSort = (s: string | null): s is Sort => SORTS.some(([id]) => id === s);

export const getImages = (
	cursor: string | null,
	limit = 48,
	f: typeof fetch = fetch,
	tag: string | null = null,
	sort: Sort = 'newest',
	seed = '',
	category: string | null = null
) => {
	const q = new URLSearchParams({ limit: String(limit) });
	if (cursor) q.set('cursor', cursor);
	if (tag) q.set('tag', tag);
	if (sort !== 'newest') q.set('sort', sort);
	if (seed) q.set('seed', seed);
	if (category) q.set('category', category);
	return f(`/api/images?${q}`).then((r) => {
		if (!r.ok) throw new ApiError(r.status);
		return r.json() as Promise<ImagePage>;
	});
};

export class ApiError extends Error {
	constructor(public status: number) {
		super(`${status}`);
	}
}

export const getTags = () => get<{ tags: TagCount[] }>('/api/tags');
export type Category = { slug: string; label: string; n: number; cover: ImageItem | null };
export const getCategories = () => get<{ categories: Category[] }>('/api/categories');

export const search = async (q: string): Promise<SearchResult> => {
	const res = await fetch(`/api/search?${new URLSearchParams({ q })}`);
	if (!res.ok) throw new ApiError(res.status);
	return res.json();
};

async function post<T>(path: string, body: unknown): Promise<T> {
	const res = await fetch(path, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'penish' },
		body: JSON.stringify(body)
	});
	if (!res.ok) throw new ApiError(res.status);
	return res.json();
}

export const postVote = (slug: string, value: -1 | 0 | 1) =>
	post<Votes>(`/api/images/${encodeURIComponent(slug)}/vote`, { value });

export const postComment = (
	slug: string,
	c: { name: string | null; body: string; turnstile: string | null }
) => post<{ ok: true }>(`/api/images/${encodeURIComponent(slug)}/comments`, c);

export const getConfig = () => get<{ turnstile_site_key: string | null }>('/api/config');
