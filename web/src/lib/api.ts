export type Haiku = { id: number | string; lines: string[] };
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
export type ImagePage = { items: ImageItem[]; next_cursor: string | null };
export type Text = { body: string; source: string; model: string | null };
export type Neighbor = { slug: string; score: number; blurhash: string; src: string };
export type ImageDetail = ImageItem & {
	caption: Text | null;
	description: Text | null;
	prompt: Text | null;
	tags: { tag: string; source: string }[];
	similar: Neighbor[];
	dissimilar: Neighbor[];
	mode: 'raw' | 'centered';
	metrics: { loneliness: number | null; typicality: number | null; surprise: number | null };
	published_at: string;
};
export type ImageRedirect = { redirect: string };

export const getImages = (cursor: string | null, limit = 48, f: typeof fetch = fetch) => {
	const q = new URLSearchParams({ limit: String(limit) });
	if (cursor) q.set('cursor', cursor);
	return f(`/api/images?${q}`).then((r) => {
		if (!r.ok) throw new Error(`images ${r.status}`);
		return r.json() as Promise<ImagePage>;
	});
};
