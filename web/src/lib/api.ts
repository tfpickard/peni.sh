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
	// 0..1, how far neglect has grown over the picture; drawn on tiles only.
	overgrowth: number;
};
// count rides only on the first page of a tag listing.
export type ImagePage = { items: ImageItem[]; next_cursor: string | null; count?: number };
export type TagCount = { tag: string; n: number };
export type SearchResult = { q: string; tags: TagCount[]; items: ImageItem[] };
export type Text = { body: string; source: string; model: string | null };
// What a strip tile needs. score is the similarity for similar and dissimilar.
export type Tile = {
	slug: string;
	blurhash: string;
	src: string;
	overgrowth: number;
	score?: number;
};
export type Neighbor = Tile & { score: number };
// An edge this picture has grown to, the stronger direction of the pair.
export type Hypha = Tile & { strength: number; layer: string };
export type Votes = { up: number; down: number; net: number; mine: -1 | 0 | 1 };
export type Comment = { name: string | null; body: string; created_at: string };
// The picture being looked at is the one a view clears, so the detail carries no level.
export type ImageDetail = Omit<ImageItem, 'overgrowth'> & {
	caption: Text | null;
	description: Text | null;
	prompt: Text | null;
	tags: { tag: string; source: string }[];
	categories: { slug: string; label: string }[];
	similar: Neighbor[];
	dissimilar: Neighbor[];
	hyphae: Hypha[];
	mode: 'raw' | 'centered';
	metrics: { loneliness: number | null; typicality: number | null; surprise: number | null };
	published_at: string;
	votes: Votes;
	comments: Comment[];
	// Neighbours in grid order, wrapping; null only when this is the sole picture.
	prev: string | null;
	next: string | null;
	// Galleries it is in now (living, then fading), then the ones it was in.
	galleries: ImageGallery[];
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
	// detail is the server's own message, when it sent one worth showing.
	constructor(
		public status: number,
		public detail: string | null = null
	) {
		super(detail ?? `${status}`);
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

// Fire and forget: keepalive lets the view land even when the visitor has just left.
export const postView = (slug: string, from: string | null) =>
	fetch(`/api/images/${encodeURIComponent(slug)}/view`, {
		method: 'POST',
		keepalive: true,
		headers: { 'x-requested-with': 'penish', 'content-type': 'application/json' },
		body: JSON.stringify({ from })
	}).then((r) => r.ok);

export const getConfig = () => get<{ turnstile_site_key: string | null }>('/api/config');

// Admin: the organism. Every call answers with the full state, so the page just swaps it in.
export type GenesisStatus = 'none' | 'queued' | 'running' | 'failed' | 'done';
export type Tunable = {
	name: string;
	value: number;
	default: number;
	lo: number;
	hi: number;
	meaning: string;
	int: boolean;
};
export type TunablesVersion = {
	version: number;
	created_at: string;
	values: Record<string, number>;
	note: string | null;
};
export type TickPoint = {
	tick: number;
	scheduled_at: string;
	simulated: boolean;
	live: number;
	in_gallery: number;
	visible: number;
	latched: number;
	mean_strength: number;
	crossings: number;
	traffic: number;
};
export type ColonyRow = {
	id: number;
	layer: string;
	state: string;
	serial: number | null;
	title: string | null;
	size: number;
	born_tick: number;
	fruited_tick: number | null;
	members: { slug: string; founder: boolean }[];
};
export type ColonyEvent = {
	tick: number;
	scheduled_at: string;
	kind: string;
	colony_id: number;
	layer: string;
	slug: string | null;
	other_colony_id: number | null;
	simulated: boolean;
};
export type Mycelium = {
	running: boolean;
	tick: number | null;
	next_due: string | null;
	behind: number;
	graph_version: number;
	graph_dirty: boolean;
	public: boolean;
	// Why a full genesis would be refused now, if it would.
	genesis_refused: 'public' | 'running' | null;
	simulated_through: number | null;
	genesis: { status: GenesisStatus; error: string | null };
	tunables: Tunable[];
	tunables_version: number | null;
	history: TunablesVersion[];
	series: TickPoint[];
	colonies: { forming: ColonyRow[]; living: ColonyRow[] };
	events: ColonyEvent[];
	facets: Facets;
};
export type FacetRecipe = 'split' | 'extract';
export type Facets = {
	active: FacetRecipe;
	// Live pictures with all four facets and vectors of each recipe, and live in all.
	counts: { split: number; extract: number; live: number };
	queued: { split: number; extract: number };
	cap: { used_today: number; daily_calls: number };
};
export type MyceliumAction = 'start' | 'pause' | 'regrow' | 'reset' | 'genesis' | 'extract_all';

async function mycelium(path: string, body?: unknown): Promise<Mycelium> {
	const res = await fetch(`/api/admin/mycelium${path}`, {
		method: body === undefined ? 'GET' : 'POST',
		credentials: 'same-origin',
		headers: { 'X-Requested-With': 'penish', 'Content-Type': 'application/json' },
		body: body === undefined ? undefined : JSON.stringify(body)
	});
	if (!res.ok) {
		// A refused tunable or genesis says why in detail; anything else is just a status.
		let detail: string | null = null;
		try {
			const j = await res.json();
			if (typeof j?.detail === 'string') detail = j.detail;
		} catch {
			detail = null;
		}
		throw new ApiError(res.status, detail);
	}
	return res.json();
}

export const getMycelium = () => mycelium('');
export const myceliumAction = (a: MyceliumAction) => mycelium(`/${a}`, {});
export const setTunables = (values: Record<string, number>, note: string | null) =>
	mycelium('/tunables', note ? { values, note } : { values });
export const setFacetSource = (recipe: FacetRecipe) => mycelium('/facet_source', { recipe });
// Only facet layers: a replay of those alone, allowed after galleries are public.
export const facetGenesis = (layers: string[]) => mycelium('/genesis', { layers });

// Galleries. Mirrors penish/api/routes_galleries.py; nothing is served before the first
// gallery is public, so every list here may simply be empty.
export type GalleryState = 'living' | 'fading' | 'dead';
export type GalleryRef = { serial: number; title: string };
export type ImageGallery = GalleryRef & { state: GalleryState; current: boolean };
export type GalleryCard = GalleryRef & {
	state: GalleryState;
	// whole, subject, setting, style, mood or need.
	layer: string;
	// Members now, or at the peak for a dead gallery.
	size: number;
	fruited_at: string | null;
	died_at: string | null;
	cause: string | null;
	cover: ImageItem | null;
	age_days: number;
};
export type GalleryMember = ImageItem & {
	caption: string | null;
	founder: boolean;
	// Other living or fading galleries the picture is in now.
	others: GalleryRef[];
};
// A tombstone keeps its peak members; one no longer live is only the slug it had.
export type FormerMember =
	| (ImageItem & { caption: string | null; live: true })
	| { slug: string; live: false };
export type GalleryEdge = { a: string; b: string; strength: number };
// total_strength is null on ticks recorded before it was kept.
export type GalleryPoint = {
	tick: number;
	size: number;
	mean_strength: number;
	total_strength: number | null;
};
export type GalleryEvent = {
	at: string;
	kind: string;
	slug: string | null;
	other: GalleryRef | null;
};
export type GalleryShare = GalleryRef & { n: number };
export type Gallery = GalleryRef & {
	state: GalleryState;
	layer: string;
	size: number;
	peak: number;
	formed_at: string | null;
	fruited_at: string | null;
	faded_at: string | null;
	died_at: string | null;
	cause: string | null;
	age_days: number;
	cover: ImageItem | null;
	parent: GalleryRef | null;
	absorbed_by: GalleryRef | null;
	children: GalleryRef[];
	members: GalleryMember[];
	former_members: FormerMember[];
	edges: GalleryEdge[];
	series: GalleryPoint[];
	// Newest first.
	events: GalleryEvent[];
	shares: GalleryShare[];
};
export type LogEvent = {
	id: number;
	at: string;
	kind: 'fruited' | 'joined' | 'left' | 'died';
	cause: string | null;
	gallery: GalleryRef;
	slug: string | null;
	other: GalleryRef | null;
};
export type MyceliumLog = { events: LogEvent[]; next: number | null };

export const getGalleries = (state: GalleryState | 'all' = 'all', f: typeof fetch = fetch) =>
	f(`/api/galleries${state === 'all' ? '' : `?state=${state}`}`).then((r) => {
		if (!r.ok) throw new ApiError(r.status);
		return r.json() as Promise<GalleryCard[]>;
	});

// null for an address that is not a gallery, so the page can say so.
export const getGallery = (serial: string, f: typeof fetch = fetch) =>
	f(`/api/galleries/${encodeURIComponent(serial)}`).then((r) => {
		if (r.status === 404) return null;
		if (!r.ok) throw new ApiError(r.status);
		return r.json() as Promise<Gallery>;
	});

export const getMyceliumLog = (before: number | null = null) =>
	get<MyceliumLog>(`/api/mycelium/log${before === null ? '' : `?before=${before}`}`);
