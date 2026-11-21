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
	// A note job for this leaf waits or is running.
	note_queued: boolean;
};
// `queued`: no usable vector and the worker has the embedding queued or running. `stale`:
// no usable vector and nothing queued (a dry run queues it).
export type AnchorState = 'embedded' | 'queued' | 'stale';
export type AdminBook = {
	number: number;
	title: string | null;
	anchor_text: string;
	tag_hints: string[];
	// Embedded under today's model.
	anchored: boolean;
	anchor_state: AnchorState;
	// Null with a vector: embedded before the time was kept.
	anchor_embedded_at: string | null;
	// Each hint with how many of the collection's leaves carry a tag it matches.
	hint_leaves: Record<string, number>;
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
	// How the book was decided, on rows that decided one.
	chosen_by: 'score' | 'owner' | 'judge' | null;
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
	tuning_limits: Record<'anchor_k' | 'tag_alpha' | 'fill_beta' | 'book_margin', [number, number]>;
	last_resort_at: string | null;
	resort: { status: string; last_error: string | null } | null;
	books: AdminBook[];
	requests: FilingRequest[];
	// Uploads for this collection that are not leaves yet; the server omits any already in `requests`.
	uploads: Upload[];
	// The newest 20, or one leaf's newest 20 when asked with `leaf`.
	history: HistoryRow[];
	// Failed judgements among the last 20 filings, by kind (`refused`, `timeout`, ...).
	judge_failures: Record<string, number>;
};
export type ProposedMove = {
	image_id: number;
	slug: string | null;
	thumb: string | null;
	before: [number, number];
	after: [number, number];
	// How far the leaf's new book scores above the best other book; null with no vector.
	margin: number | null;
	// Re-judging only: what the judge said of a move to another book; null on a shift within one.
	reason?: string | null;
	runner_up?: number | null;
};
// A dry run's answer. Sent back whole, it applies exactly these moves or is refused `changed`.
export type Plan = { plan_id: string; moves: ProposedMove[] };
// A leaf re-judging leaves where it is: its judged book is full (`book` set), or the judge
// gave no judgement (`why` says what happened instead).
export type HeldLeaf = {
	image_id: number;
	slug: string | null;
	thumb: string | null;
	before: [number, number];
	book: number | null;
	why: string;
};
export type JudgePlan = Plan & { held: HeldLeaf[] };

// One book's score for a picture in its parts; they add up to `total`.
export type Terms = { number: number; cos: number; tag: number; fill: number; total: number };
export type Weighing = {
	best: number;
	runner_up: number | null;
	margin: number | null;
	books: Terms[];
};
// A judgement (book, runner_up, reason) or a failure (error).
export type JudgeSaid = {
	model?: string;
	book?: number;
	runner_up?: number;
	reason?: string;
	error?: string;
	detail?: string;
};
export type Explain = {
	image_id: number;
	slug: string | null;
	book: number;
	position: number;
	pinned: boolean;
	book_pinned: boolean;
	// The judge chose its book (the latest decision).
	judged: boolean;
	// The kept answer for the leaf and books as they are now.
	judgement: JudgeSaid | null;
	// Every book now, the leaf left out of its own; `lead` is its own book over the best other.
	now: (Weighing & { lead: number | null }) | null;
	filed: {
		at: string;
		book: number;
		position: number;
		reason: string;
		chosen_by: 'score' | 'owner' | 'judge' | null;
		scores: (Partial<Weighing> & { judge?: JudgeSaid }) | null;
	} | null;
};

const at = (slug: string) => `/${encodeURIComponent(slug)}`;

export const list = () => call<{ collections: Summary[] }>('GET', '');
// `leaf` (image id or slug) narrows the history to one leaf.
export const detail = (slug: string, leaf?: string) =>
	call<Detail>('GET', leaf ? `${at(slug)}?leaf=${encodeURIComponent(leaf)}` : at(slug));
export const explain = (slug: string, imageId: number) =>
	call<Explain>('GET', `${at(slug)}/explain?image_id=${imageId}`);
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
// Refused `anchors` while a book's anchor waits for its vector; the server queues it.
export const dryRun = (slug: string) => call<Plan>('POST', `${at(slug)}/resort`, { dry_run: true });
export const resort = (slug: string, plan: Plan) =>
	call<Detail>('POST', `${at(slug)}/resort`, { dry_run: false, ...plan });
// Refused `judging` while the worker still has leaves to judge; the server queues it.
// Refused `off` while filer.judge is off, and nothing is queued.
export const judgeRun = (slug: string) => call<JudgePlan>('POST', `${at(slug)}/judge`, { dry_run: true });
export const judgeApply = (slug: string, plan: Plan) =>
	call<Detail>('POST', `${at(slug)}/judge`, { dry_run: false, plan_id: plan.plan_id, moves: plan.moves });
