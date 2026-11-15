// Reads only: log.level and log.keep_days are runtime keys, written through admin-ops putSetting.
const H = { 'X-Requested-With': 'penish' };

async function get<T>(path: string): Promise<T> {
	const r = await fetch(`/api/admin/logs${path}`, { credentials: 'same-origin', headers: H });
	if (!r.ok) throw new Error(String(r.status));
	return (await r.json()) as T;
}

export type Source = 'api' | 'worker';
// Filter levels are minimums; critical has no runtime setting but can be filtered on.
export const FILTER_LEVELS = ['debug', 'info', 'warning', 'error', 'critical'] as const;
export type FilterLevel = (typeof FILTER_LEVELS)[number];
export const SOURCES: readonly Source[] = ['api', 'worker'];

export type Context = Record<string, unknown>;

export type Line = {
	id: number;
	at: string;
	source: Source;
	// Lowercase name, or `level N` for a level Python has no name for.
	level: string;
	logger: string;
	message: string;
	has_exc: boolean;
	context: Context | null;
};

export type Page = {
	items: Line[];
	// Pass as `before` for the next older page; null when this was the last one.
	next: number | null;
	dropped: number;
	level: string;
	keep_days: number;
	levels: string[];
};

export type Detail = Omit<Line, 'has_exc'> & { exc: string | null };

export type Filters = {
	level: FilterLevel | '';
	source: Source | '';
	logger: string;
	q: string;
	job: string;
	image: string;
};

export const noFilters = (): Filters => ({ level: '', source: '', logger: '', q: '', job: '', image: '' });

const isLevel = (s: string | null): s is FilterLevel => FILTER_LEVELS.includes(s as FilterLevel);
const isSource = (s: string | null): s is Source => SOURCES.includes(s as Source);
// The api takes only integers for these; a pasted `#4182` or `job 4182` means 4182.
const intOrEmpty = (s: string | null) => s?.match(/\d+/)?.[0] ?? '';
// Text in a job or picture box with no number in it, which the query leaves out.
export const ignored = (s: string) => s.trim() !== '' && intOrEmpty(s) === '';

export function fromQuery(p: URLSearchParams): Filters {
	const level = p.get('level');
	const source = p.get('source');
	return {
		level: isLevel(level) ? level : '',
		source: isSource(source) ? source : '',
		logger: p.get('logger')?.trim() ?? '',
		q: p.get('q')?.trim() ?? '',
		job: intOrEmpty(p.get('job')),
		image: intOrEmpty(p.get('image'))
	};
}

// Only set keys, in a fixed order, so the same filters always give the same URL.
export function toQuery(f: Filters): URLSearchParams {
	const p = new URLSearchParams();
	if (f.level) p.set('level', f.level);
	if (f.source) p.set('source', f.source);
	if (f.logger.trim()) p.set('logger', f.logger.trim());
	if (f.q.trim()) p.set('q', f.q.trim());
	const job = intOrEmpty(f.job);
	if (job) p.set('job', job);
	const image = intOrEmpty(f.image);
	if (image) p.set('image', image);
	return p;
}

export const PAGE = 100;

// `after` asks only for lines newer than that id, which is how follow polls.
export function list(
	f: Filters,
	before: number | null = null,
	limit = PAGE,
	after: number | null = null
): Promise<Page> {
	const p = toQuery(f);
	if (before !== null) p.set('before', String(before));
	if (after !== null) p.set('after', String(after));
	p.set('limit', String(limit));
	return get<Page>(`?${p}`);
}

export const detail = (id: number) => get<Detail>(`/${id}`);
