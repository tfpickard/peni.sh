<script lang="ts">
	import { onMount } from 'svelte';
	import * as cols from '../../../lib/admin-collections';
	import { roman } from '../../../lib/roman';

	let summaries = $state<cols.Summary[]>([]);
	let slug = $state('');
	let d = $state<cols.Detail | null>(null);
	let msg = $state('');
	let loaded = $state(false);

	// Drafts, so a reload after another action does not throw away what is being typed.
	type Row = { label: string; value: string };
	type Head = {
		title: string;
		abbrev: string;
		abbrev_latin: string;
		gloss: string;
		preamble: string;
		record: Row[];
		vendor_text: string;
	};
	const headOf = (x: cols.Detail): Head => ({
		title: x.title,
		abbrev: x.abbrev,
		abbrev_latin: x.abbrev_latin,
		gloss: x.gloss,
		preamble: x.preamble,
		record: x.record.map(([label, value]) => ({ label, value })),
		vendor_text: x.vendor_text ?? ''
	});
	let head = $state<Head>({
		title: '',
		abbrev: '',
		abbrev_latin: '',
		gloss: '',
		preamble: '',
		record: [],
		vendor_text: ''
	});
	let tuning = $state<cols.Tuning | null>(null);
	type BookDraft = { title: string; anchor_text: string; tag_hints: string };
	let books = $state<Record<number, BookDraft>>({});

	// The leaf picked in a book's grid, and where to move it.
	let picked = $state<{ book: number; leaf: cols.AdminLeaf } | null>(null);
	let moveBook = $state('');
	let moveLeaf = $state('');
	let proposed = $state<cols.ProposedMove[] | null>(null);

	const bookDraft = (b: cols.AdminBook): BookDraft => ({
		title: b.title ?? '',
		anchor_text: b.anchor_text,
		tag_hints: b.tag_hints.join(', ')
	});

	function take(x: cols.Detail, fresh = false) {
		if (fresh || !d) {
			head = headOf(x);
			tuning = { ...x.tuning };
			books = Object.fromEntries(x.books.map((b) => [b.number, bookDraft(b)]));
		}
		d = x;
		if (picked) {
			const b = x.books.find((bk) => bk.leaves.some((l) => l.image_id === picked?.leaf.image_id));
			const l = b?.leaves.find((lf) => lf.image_id === picked?.leaf.image_id);
			picked = b && l ? { book: b.number, leaf: l } : null;
		}
	}

	async function open(s: string) {
		slug = s;
		picked = null;
		proposed = null;
		try {
			take(await cols.detail(s), true);
		} catch {
			msg = 'failed';
		}
	}

	onMount(async () => {
		try {
			summaries = (await cols.list()).collections;
			if (summaries.length) await open(summaries[0].slug);
		} catch {
			msg = 'failed';
		}
		loaded = true;
	});

	async function act(fn: () => Promise<cols.Detail>, fresh = false) {
		msg = '';
		try {
			take(await fn(), fresh);
		} catch (e) {
			msg = e instanceof cols.HttpError && e.detail ? e.detail : 'failed';
		}
	}

	const saveHead = () =>
		act(
			() =>
				cols.edit(slug, {
					title: head.title,
					abbrev: head.abbrev,
					abbrev_latin: head.abbrev_latin,
					gloss: head.gloss,
					preamble: head.preamble,
					record: head.record.map((r) => [r.label, r.value] as [string, string]),
					vendor_text: head.vendor_text
				}),
			true
		);
	// Rows for a value as it wraps at the field's width (about 55 characters), so a long
	// provenance line is not cut off; browsers with field-sizing size it exactly anyway.
	const fit = (v: string) =>
		v.split('\n').reduce((n, line) => n + Math.max(1, Math.ceil(line.length / 55)), 0);
	const addRow = () => (head.record = [...head.record, { label: '', value: '' }]);
	const dropRow = (i: number) => (head.record = head.record.filter((_, k) => k !== i));
	function shift(i: number, by: -1 | 1) {
		const j = i + by;
		if (j < 0 || j >= head.record.length) return;
		const next = [...head.record];
		[next[i], next[j]] = [next[j], next[i]];
		head.record = next;
	}
	const writeMissing = () => act(() => cols.writeMissingNotes(slug));
	const flipPublished = () => act(() => cols.edit(slug, { published: !d?.published }));
	const saveTuning = () => tuning && act(() => cols.edit(slug, { tuning: { ...tuning! } }), true);
	function saveBook(n: number) {
		const b = books[n];
		const hints = b.tag_hints
			.split(/[,\n]/)
			.map((t) => t.trim())
			.filter(Boolean);
		return act(() => cols.editBook(slug, n, { title: b.title, anchor_text: b.anchor_text, tag_hints: hints }), true);
	}
	const bookDirty = (b: cols.AdminBook) => {
		const x = books[b.number];
		const y = bookDraft(b);
		return !!x && (x.title !== y.title || x.anchor_text !== y.anchor_text || x.tag_hints !== y.tag_hints);
	};
	const headDirty = $derived(!!d && JSON.stringify(head) !== JSON.stringify(headOf(d)));

	function pick(book: number, leaf: cols.AdminLeaf) {
		picked = picked?.leaf.image_id === leaf.image_id ? null : { book, leaf };
		moveBook = String(book);
		moveLeaf = '';
	}
	const move = () =>
		picked &&
		act(() =>
			cols.place(slug, picked!.leaf.image_id, Number(moveBook), moveLeaf ? Number(moveLeaf) : null)
		);
	const pinFlip = () => picked && act(() => cols.pin(slug, picked!.leaf.image_id, !picked!.leaf.pinned));
	async function unfile(imageId: number) {
		await act(() => cols.unfile(slug, imageId));
		if (picked?.leaf.image_id === imageId) picked = null;
	}

	async function dry() {
		msg = '';
		try {
			proposed = (await cols.dryRun(slug)).moves;
		} catch (e) {
			msg = e instanceof cols.HttpError && e.detail ? e.detail : 'failed';
		}
	}
	async function resortNow() {
		proposed = null;
		await act(() => cols.resort(slug));
	}

	// While a re-sort or a filing waits, keep the page current.
	const busy = $derived(
		!!d &&
			(d.resort?.status === 'queued' ||
				d.resort?.status === 'running' ||
				d.requests.some((r) => r.job === 'queued' || r.job === 'running') ||
				d.notes_queued > 0 ||
				d.books.some((b) => !b.anchored && b.anchor_text))
	);
	$effect(() => {
		if (!busy || !slug) return;
		const t = setInterval(() => {
			if (document.visibilityState === 'visible') act(() => cols.detail(slug));
		}, 3000);
		return () => clearInterval(t);
	});

	const place = (b: number | null, p: number | null) =>
		b === null ? '' : p === null ? roman(b) : `${roman(b)} ${p}`;
	const cells = (b: cols.AdminBook) => {
		const out: (cols.AdminLeaf | null)[] = Array(d?.leaves_per_book ?? 0).fill(null);
		for (const l of b.leaves) out[l.position - 1] = l;
		return out;
	};
	const pair = (x: [number, number] | null) => (x ? `${roman(x[0])} ${x[1]}` : '');
	const when = (s: string) => s.replace('T', ' ').slice(0, 16);
</script>

{#if loaded && !summaries.length}
	<p class="dim">0 collections</p>
{/if}

{#if summaries.length > 1}
	<div class="tabs">
		{#each summaries as s (s.slug)}
			<button class:on={s.slug === slug} class:link={s.slug !== slug} onclick={() => open(s.slug)}>
				{s.slug}
			</button>
		{/each}
	</div>
{/if}

{#if d}
	<section class="head">
		<label>
			<span class="l">title</span>
			<input bind:value={head.title} />
		</label>
		<div class="pair">
			<label>
				<span class="l">abbreviation</span>
				<input bind:value={head.abbrev} />
			</label>
			<label>
				<span class="l">latin</span>
				<input bind:value={head.abbrev_latin} />
			</label>
		</div>
		<label>
			<span class="l">gloss</span>
			<input bind:value={head.gloss} />
		</label>
		<label>
			<span class="l">preamble</span>
			<textarea rows="5" bind:value={head.preamble}></textarea>
		</label>

		<div class="record">
			<span class="l">record</span>
			{#each head.record as r, i (i)}
				<div class="rec">
					<input class="lab" bind:value={r.label} aria-label="label {i + 1}" placeholder="label" />
					<textarea
						rows={fit(r.value)}
						bind:value={r.value}
						aria-label="value {i + 1}"
						placeholder="value, one line per line"
					></textarea>
					<div class="recacts">
						<button class="link" disabled={i === 0} onclick={() => shift(i, -1)}>up</button>
						<button class="link" disabled={i === head.record.length - 1} onclick={() => shift(i, 1)}
							>down</button
						>
						<button class="link" onclick={() => dropRow(i)}>remove</button>
					</div>
				</div>
			{/each}
			<div class="row"><button class="link" onclick={addRow}>add row</button></div>
		</div>

		<label>
			<span class="l">vendor's description</span>
			<textarea rows="3" bind:value={head.vendor_text}></textarea>
		</label>
		<div class="row">
			<button class="go" disabled={!headDirty} onclick={saveHead}>save</button>
			<button class:link={d.published} onclick={flipPublished}>
				{d.published ? 'unpublish' : 'publish'}
			</button>
			<span class="dim">{d.published ? 'published' : 'not published'}</span>
			<a href="/collections/{d.slug}" class="dim">view</a>
		</div>
	</section>

	{#if tuning}
		<h2>filing</h2>
		<div class="tuning">
			{#each ['anchor_k', 'tag_alpha', 'fill_beta', 'book_margin'] as const as k (k)}
				<label>
					<span class="l">{k.replace('_', ' ')} <span class="dim">{d.tuning_default[k]}</span></span>
					<input type="number" step="0.01" min="0" bind:value={tuning[k]} />
				</label>
			{/each}
			<label class="tick">
				<input type="checkbox" bind:checked={tuning.weekly_resort} />
				weekly re-sort
			</label>
		</div>
		<div class="row">
			<button onclick={saveTuning}>save</button>
		</div>
	{/if}

	<h2>conservator notes</h2>
	<div class="row">
		<button onclick={writeMissing}>write missing notes</button>
		{#if d.notes_queued}<span class="dim">{d.notes_queued} queued</span>{/if}
		<span class="dim">
			{d.books.reduce((n, b) => n + b.leaves.filter((l) => l.note).length, 0)} of
			{d.books.reduce((n, b) => n + b.leaves.length, 0)} leaves noted
		</span>
	</div>

	<h2>re-sort</h2>
	<div class="row">
		<button onclick={dry}>dry run</button>
		<button
			disabled={d.resort?.status === 'queued' || d.resort?.status === 'running'}
			onclick={resortNow}>re-sort now</button
		>
		{#if d.resort}<span class="dim" class:hot={d.resort.status === 'failed'}>{d.resort.status}</span>{/if}
		{#if d.last_resort_at}<span class="dim">last {when(d.last_resort_at)}</span>{/if}
	</div>
	{#if d.resort?.last_error && d.resort.status === 'failed'}<p class="hot err">{d.resort.last_error}</p>{/if}
	{#if proposed}
		{#if proposed.length}
			<table class="moves">
				<tbody>
					{#each proposed as m (m.image_id)}
						<tr>
							<td><a href="/admin/pictures/{m.image_id}">{m.slug ?? m.image_id}</a></td>
							<td class="dim">{pair(m.before)}</td>
							<td>{pair(m.after)}</td>
							{#if m.before && m.after && m.before[0] !== m.after[0]}<td class="hot">book</td>{/if}
						</tr>
					{/each}
				</tbody>
			</table>
		{:else}
			<p class="dim">0 moves</p>
		{/if}
	{/if}

	{#if msg}<p class="hot">{msg}</p>{/if}

	<h2>unfiled</h2>
	{#if d.requests.length}
		<ul class="plain">
			{#each d.requests as r (r.image_id)}
				<li class="row">
					{#if r.thumb}<img class="thumb" src={r.thumb} alt="" />{/if}
					<a href="/admin/pictures/{r.image_id}">{r.slug ?? r.image_id}</a>
					<span class="dim">{r.status}</span>
					{#if r.book !== null}<span>{place(r.book, r.position)}</span>{/if}
					{#if r.job}<span class="dim">{r.job}</span>{/if}
					{#if r.error}<span class="hot">{r.error}</span>{/if}
					<button onclick={() => act(() => cols.retry(slug, r.image_id))}>retry</button>
					<button class="link" onclick={() => unfile(r.image_id)}>drop</button>
				</li>
			{/each}
		</ul>
	{:else}
		<p class="dim">0 unfiled</p>
	{/if}

	{#each d.books as b (b.number)}
		<section class="book">
			<h2>
				book {roman(b.number)}
				<span class="dim">{b.leaves.length} of {d.leaves_per_book}</span>
				{#if b.anchor_text && !b.anchored}<span class="dim">anchor queued</span>{/if}
			</h2>
			{#if books[b.number]}
				<label>
					<span class="l">title</span>
					<input bind:value={books[b.number].title} />
				</label>
				<label>
					<span class="l">anchor</span>
					<textarea rows="3" bind:value={books[b.number].anchor_text}></textarea>
				</label>
				<label>
					<span class="l">tag hints</span>
					<input bind:value={books[b.number].tag_hints} />
				</label>
				<div class="row">
					<button disabled={!bookDirty(b)} onclick={() => saveBook(b.number)}>save</button>
				</div>
			{/if}
			<div class="grid" style:--n={Math.min(d.leaves_per_book, 11)}>
				{#each cells(b) as l, i (i)}
					{#if l}
						<button
							class="cell"
							class:sel={picked?.leaf.image_id === l.image_id}
							class:pin={l.pinned}
							title="{i + 1} {l.slug ?? ''}"
							onclick={() => pick(b.number, l)}
						>
							{#if l.thumb}<img src={l.thumb} alt="" loading="lazy" />{/if}
						</button>
					{:else}
						<span class="cell empty" title={String(i + 1)}></span>
					{/if}
				{/each}
			</div>
			{#if picked && picked.book === b.number}
				<div class="picked">
					<div class="row">
						<a href="/admin/pictures/{picked.leaf.image_id}">{picked.leaf.slug ?? picked.leaf.image_id}</a>
						<span>leaf {picked.leaf.position}</span>
						<span class="dim">
							{picked.leaf.pinned ? 'pinned' : picked.leaf.book_pinned ? 'book pinned' : 'auto'}
						</span>
						{#if picked.leaf.status !== 'live'}<span class="dim">{picked.leaf.status}</span>{/if}
					</div>
					<div class="row">
						<select aria-label="book" bind:value={moveBook}>
							{#each Array.from({ length: d.book_count }, (_, k) => k + 1) as n (n)}
								<option value={String(n)}>{roman(n)}</option>
							{/each}
						</select>
						<input
							class="num"
							type="number"
							min="1"
							max={d.leaves_per_book}
							placeholder="leaf"
							aria-label="leaf"
							bind:value={moveLeaf}
						/>
						<button onclick={move}>move</button>
						<button onclick={pinFlip}>{picked.leaf.pinned ? 'unpin' : 'pin'}</button>
						<button class="link" onclick={() => unfile(picked!.leaf.image_id)}>unfile</button>
					</div>
				</div>
			{/if}
		</section>
	{/each}

	<h2>history</h2>
	{#if d.history.length}
		<table>
			<tbody>
				{#each d.history as h (h.id)}
					<tr>
						<td class="dim">{when(h.at)}</td>
						<td><a href="/admin/pictures/{h.image_id}">{h.slug ?? h.image_id}</a></td>
						<td class="dim">{place(h.from_book, h.from_position)}</td>
						<td>{h.to_book === null ? 'out' : place(h.to_book, h.to_position)}</td>
						<td class="dim">{h.reason}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{:else}
		<p class="dim">0 moves</p>
	{/if}
{/if}

<style>
	label {
		display: flex;
		flex-direction: column;
		margin-top: 0.75rem;
		max-width: 40rem;
	}
	.l {
		color: var(--foreground);
	}
	textarea {
		resize: vertical;
	}
	.pair {
		display: flex;
		flex-wrap: wrap;
		gap: 0 1.5rem;
	}
	.record {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		margin-top: 0.75rem;
		max-width: 40rem;
	}
	.rec {
		display: grid;
		grid-template-columns: 9rem 1fr;
		gap: 0.35rem 0.75rem;
	}
	.rec textarea {
		field-sizing: content;
	}
	.recacts {
		grid-column: 2;
		display: flex;
		gap: 1.25rem;
		font-size: 0.875rem;
	}
	@media (max-width: 30rem) {
		.rec {
			grid-template-columns: 1fr;
		}
		.recacts {
			grid-column: 1;
		}
	}
	.row {
		margin-top: 0.75rem;
	}
	.tuning {
		display: flex;
		flex-wrap: wrap;
		gap: 0 1.5rem;
		align-items: flex-end;
	}
	.tuning input[type='number'] {
		width: 7rem;
	}
	.tick {
		flex-direction: row;
		align-items: center;
		gap: 0.5rem;
		color: var(--muted-foreground);
	}
	.tick input {
		width: 1rem;
		height: 1rem;
		margin: 0;
		accent-color: var(--primary);
	}
	.err {
		font-size: 0.875rem;
		overflow-wrap: anywhere;
	}
	.moves {
		margin-top: 0.75rem;
		width: auto;
	}
	.book h2 {
		display: flex;
		gap: 1rem;
		align-items: baseline;
	}
	.grid {
		display: grid;
		grid-template-columns: repeat(var(--n), 1fr);
		gap: 0.25rem;
		max-width: 40rem;
		margin-top: 1rem;
	}
	.cell {
		display: block;
		aspect-ratio: 1;
		min-width: 0;
		padding: 0;
		overflow: hidden;
		outline: 1px solid var(--ink-800);
		outline-offset: -1px;
	}
	button.cell {
		outline: 0;
		background: var(--muted);
	}
	button.cell.pin {
		outline: 1px solid var(--secondary);
		outline-offset: -1px;
	}
	button.cell.sel {
		outline: 2px solid var(--primary);
		outline-offset: -2px;
	}
	.cell img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.picked {
		margin-top: 0.75rem;
	}
	.num {
		width: 5rem;
	}
	td a {
		overflow-wrap: anywhere;
	}
</style>
