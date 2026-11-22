<script lang="ts">
	import { onMount } from 'svelte';
	import { beforeNavigate } from '$app/navigation';
	import * as cols from '../../../lib/admin-collections';
	import * as dr from '../../../lib/collection-drafts';
	import type { Key } from '../../../lib/collection-drafts';
	import { roman } from '../../../lib/roman';
	import Grow from '../../../lib/components/admin/Grow.svelte';
	import RecordList from '../../../lib/components/RecordList.svelte';

	let summaries = $state<cols.Summary[]>([]);
	let slug = $state('');
	let d = $state<cols.Detail | null>(null);
	let msg = $state('');
	let loaded = $state(false);

	// Drafts. Each section (the collection's fields, its record, tuning, each book, each
	// leaf's note) is edited, saved and lost on its own: saving one never touches another's
	// draft, and a reload after some other action keeps what is being typed.
	let fields = $state<dr.Fields>({
		title: '',
		abbrev: '',
		abbrev_latin: '',
		gloss: '',
		preamble: '',
		vendor_text: ''
	});
	let record = $state<dr.Row[]>([]);
	let tuning = $state<cols.Tuning | null>(null);
	let books = $state<Record<number, dr.BookDraft>>({});
	// By image id.
	let notes = $state<Record<number, string>>({});
	// What a section's last save or note request answered, flat, beside that section.
	let errs = $state<Record<string, string>>({});
	let saving = $state(false);
	// Which books show their notes.
	let showNotes = $state<Record<number, boolean>>({});

	const leavesOf = (x: cols.Detail) => x.books.flatMap((b) => b.leaves);
	const keysOf = (x: cols.Detail): Key[] => [
		'fields',
		'record',
		'tuning',
		...x.books.map((b) => dr.bookKey(b.number)),
		...leavesOf(x).map((l) => dr.noteKey(l.image_id))
	];
	// The sections whose draft differs from `x`.
	function dirtyIn(x: cols.Detail): Key[] {
		const out: Key[] = [];
		if (!dr.same(fields, dr.fieldsOf(x))) out.push('fields');
		if (!dr.same(record, dr.recordOf(x))) out.push('record');
		if (tuning && !dr.same(tuning, x.tuning)) out.push('tuning');
		for (const b of x.books) {
			if (dr.bookDirty(books[b.number], b)) out.push(dr.bookKey(b.number));
			for (const l of b.leaves) if (dr.noteDirty(notes[l.image_id], l.note)) out.push(dr.noteKey(l.image_id));
		}
		return out;
	}
	const dirtyKeys = $derived(d ? dirtyIn(d) : []);
	const dirty = $derived(new Set<Key>(dirtyKeys));
	const bookNotesDirty = (b: cols.AdminBook) => b.leaves.some((l) => dirty.has(dr.noteKey(l.image_id)));

	// What a section's draft is now, to tell later whether it moved while a save was out.
	function snap(k: Key): string {
		if (k === 'fields') return JSON.stringify(fields);
		if (k === 'record') return JSON.stringify(record);
		if (k === 'tuning') return JSON.stringify(tuning);
		const n = Number(k.slice(k.indexOf(':') + 1));
		return JSON.stringify(k.startsWith('book:') ? books[n] : notes[n]);
	}
	function adopt(k: Key, x: cols.Detail) {
		if (k === 'fields') fields = dr.fieldsOf(x);
		else if (k === 'record') record = dr.recordOf(x);
		else if (k === 'tuning') tuning = { ...x.tuning };
		else {
			const n = Number(k.slice(k.indexOf(':') + 1));
			if (k.startsWith('book:')) {
				const b = x.books.find((bk) => bk.number === n);
				if (b) books[n] = dr.bookOf(b);
			} else {
				const l = leavesOf(x).find((lf) => lf.image_id === n);
				if (l) notes[n] = l.note ?? '';
			}
		}
	}

	// A section takes the server's answer when it was clean, so a model note arriving shows
	// up; a dirty one keeps its draft, and so does the one just saved if it was typed in
	// again while the save was out.
	// `refreshed`: x was asked for with the history filter, so its history is the one to show.
	// An answer for a collection that is not the open one is dropped.
	function take(
		x: cols.Detail,
		opts: { open?: boolean; refreshed?: boolean; saved?: { key: Key; sent: string } } = {}
	) {
		if (!opts.open && d && x.slug !== slug) return;
		const was = new Set<Key>(d ? dirtyIn(d) : []);
		const fresh = opts.open || !d;
		d = x;
		if (opts.open) errs = {};
		for (const k of keysOf(x)) {
			const own = opts.saved?.key === k && snap(k) === opts.saved.sent;
			if (fresh || !was.has(k) || own) adopt(k, x);
		}
		// Every write answers unfiltered, so a filtered history asks again.
		if (opts.refreshed || !histLeaf) hist = x.history;
		else void loadHist();
		if (picked) {
			const b = x.books.find((bk) => bk.leaves.some((l) => l.image_id === picked?.leaf.image_id));
			const l = b?.leaves.find((lf) => lf.image_id === picked?.leaf.image_id);
			picked = b && l ? { book: b.number, leaf: l } : null;
		}
	}

	// The filer's weights, each with the line that says what it does.
	const WEIGHTS = [
		['anchor_k', 'anchor k', 'how many leaves the written anchor counts as'],
		['tag_alpha', 'tag alpha', 'weight of tag-hint matches'],
		['fill_beta', 'fill beta', 'how hard fuller books push back'],
		['book_margin', 'book margin', 'how much better another book must score before a leaf moves']
	] as const;

	// The leaf picked in a book's grid, why it is there, and where to move it.
	let picked = $state<{ book: number; leaf: cols.AdminLeaf } | null>(null);
	let explained = $state<cols.Explain | null>(null);
	let moveBook = $state('');
	let moveLeaf = $state('');
	// The re-sort's dry run or the judge's; each applies only through its own button.
	let proposed = $state<((cols.Plan & { kind: 'resort' }) | (cols.JudgePlan & { kind: 'judge' })) | null>(null);
	let judging = $state(false);
	// The history as shown: the collection's newest, or one leaf's when filtered.
	let hist = $state<cols.HistoryRow[]>([]);
	let histLeaf = $state('');
	let histInput = $state('');

	// The latest open wins; `slug` moves only with the drafts, so a detail that fails to
	// arrive leaves the form saving to the collection it shows.
	let opening = 0;
	async function open(s: string) {
		// Another collection's drafts would take the place of these.
		if (dirtyKeys.length && s !== slug && !confirm('unsaved changes. leave?')) return;
		const n = ++opening;
		let x: cols.Detail;
		try {
			x = await cols.detail(s);
		} catch {
			if (n === opening) msg = 'failed';
			return;
		}
		if (n !== opening) return;
		slug = s;
		picked = null;
		explained = null;
		proposed = null;
		showNotes = {};
		histLeaf = histInput = '';
		take(x, { open: true });
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

	const flat = (e: unknown) => (e instanceof cols.HttpError && e.detail ? e.detail : 'failed');

	// Any action that is not a section's save; it leaves every draft as it is.
	async function act(fn: () => Promise<cols.Detail>) {
		const s = slug;
		msg = '';
		try {
			const x = await fn();
			if (s === slug) take(x);
		} catch (e) {
			if (s === slug) msg = flat(e);
		}
	}

	function send(k: Key): Promise<cols.Detail> {
		if (k === 'fields') return cols.edit(slug, { ...fields });
		if (k === 'record') {
			return cols.edit(slug, { record: record.map((r) => [r.label, r.value] as [string, string]) });
		}
		if (k === 'tuning') return cols.edit(slug, { tuning: { ...tuning! } });
		const n = Number(k.slice(k.indexOf(':') + 1));
		if (k.startsWith('book:')) {
			const b = books[n];
			return cols.editBook(slug, n, { title: b.title, anchor_text: b.anchor_text, tag_hints: dr.hintsOf(b) });
		}
		return cols.setNote(slug, n, notes[n]);
	}
	// A failed section stays dirty and says why beside itself; the rest are unaffected.
	async function save(k: Key): Promise<boolean> {
		// A leaf unfiled since the list was made has no section to save.
		if (!d || !keysOf(d).includes(k)) return true;
		const s = slug;
		const sent = snap(k);
		errs[k] = '';
		try {
			const x = await send(k);
			if (s === slug) take(x, { saved: { key: k, sent } });
			return true;
		} catch (e) {
			if (s === slug) errs[k] = flat(e);
			return false;
		}
	}
	// One after another through the same endpoints; the list is fixed before the first goes.
	async function saveAll() {
		const s = slug;
		saving = true;
		try {
			for (const k of [...dirtyKeys]) {
				if (s !== slug) break;
				await save(k);
			}
		} finally {
			saving = false;
		}
	}
	async function saveOne(k: Key) {
		saving = true;
		try {
			await save(k);
		} finally {
			saving = false;
		}
	}
	async function writeNote(id: number) {
		const k = dr.noteKey(id);
		const s = slug;
		errs[k] = '';
		try {
			const x = await cols.writeNote(s, id);
			if (s === slug) take(x);
		} catch (e) {
			if (s === slug) errs[k] = flat(e);
		}
	}

	// A leave warning: reloads and closing by beforeunload, the admin's own links and back
	// and forward by beforeNavigate. Code that navigates on its own, such as logout, is let
	// through.
	function warn(e: BeforeUnloadEvent) {
		if (!dirtyKeys.length) return;
		e.preventDefault();
		// Older Safari and Chrome only ask when this is set.
		e.returnValue = '';
	}
	beforeNavigate((nav) => {
		if (!dirtyKeys.length || nav.willUnload) return;
		if (nav.type !== 'link' && nav.type !== 'popstate') return;
		// Against where the navigation started, not location, as the upload form's does.
		if (nav.to?.url.pathname === nav.from?.url.pathname) return;
		if (!confirm('unsaved changes. leave?')) nav.cancel();
	});

	const addRow = () => (record = [...record, { label: '', value: '' }]);
	const dropRow = (i: number) => (record = record.filter((_, k) => k !== i));
	function shift(i: number, by: -1 | 1) {
		const j = i + by;
		if (j < 0 || j >= record.length) return;
		const next = [...record];
		[next[i], next[j]] = [next[j], next[i]];
		record = next;
	}
	// The preview's pencil: the row's value, where the text is.
	function editRow(i: number) {
		const el = document.getElementById(`rec-value-${i}`);
		el?.scrollIntoView({ block: 'center' });
		el?.focus();
	}
	const writeMissing = () => act(() => cols.writeMissingNotes(slug));
	const flipPublished = () => act(() => cols.edit(slug, { published: !d?.published }));

	function addHints(n: number) {
		const b = books[n];
		b.tag_hints = [...new Set([...b.tag_hints, ...dr.hintsIn(b.adding)])];
		b.adding = '';
	}
	const dropHint = (n: number, h: string) =>
		(books[n].tag_hints = books[n].tag_hints.filter((x) => x !== h));
	function hintKey(e: KeyboardEvent, n: number) {
		if (e.key === 'Enter' || e.key === ',') {
			e.preventDefault();
			addHints(n);
		}
	}

	async function explain(imageId: number) {
		try {
			const got = await cols.explain(slug, imageId);
			if (picked?.leaf.image_id === imageId) explained = got;
		} catch {
			explained = null;
		}
	}
	function pick(book: number, leaf: cols.AdminLeaf) {
		picked = picked?.leaf.image_id === leaf.image_id ? null : { book, leaf };
		explained = null;
		moveBook = String(book);
		moveLeaf = '';
		if (picked) void explain(leaf.image_id);
	}
	// After a move or a pin the leaf's scores are different, so they are asked for again.
	async function onLeaf(fn: (id: number) => Promise<cols.Detail>) {
		if (!picked) return;
		const id = picked.leaf.image_id;
		await act(() => fn(id));
		if (picked?.leaf.image_id === id) await explain(id);
	}
	const move = () =>
		onLeaf((id) => cols.place(slug, id, Number(moveBook), moveLeaf ? Number(moveLeaf) : null));
	const pinFlip = () => onLeaf((id) => cols.pin(slug, id, !picked!.leaf.pinned));
	async function unfile(imageId: number) {
		// An unfiled leaf has no note section left to keep the draft in.
		if (dirty.has(dr.noteKey(imageId)) && !confirm('unsaved note. unfile anyway?')) return;
		await act(() => cols.unfile(slug, imageId));
		if (picked?.leaf.image_id === imageId) {
			picked = null;
			explained = null;
		}
	}

	async function loadHist() {
		const s = slug;
		const leaf = histLeaf;
		try {
			const got = (await cols.detail(s, leaf)).history;
			if (s === slug && leaf === histLeaf) hist = got;
		} catch {
			msg = 'failed';
		}
	}
	function filterHist(leaf: string) {
		histInput = histLeaf = leaf.trim();
		if (histLeaf) void loadHist();
		else hist = d?.history ?? [];
	}

	async function dry() {
		msg = '';
		try {
			proposed = { ...(await cols.dryRun(slug)), kind: 'resort' };
		} catch (e) {
			msg = e instanceof cols.HttpError && e.detail ? e.detail : 'failed';
		}
	}
	// Applies the plan the dry run showed, never a fresh one.
	async function resortNow() {
		const plan = proposed;
		if (!plan || plan.kind !== 'resort') return;
		proposed = null;
		await act(() => cols.resort(slug, plan));
	}
	// The worker judges whatever has no judgement yet; until it has, the same dry run is
	// refused `judging`, so it is asked again every few seconds for a few minutes.
	async function judgeDry() {
		const s = slug;
		msg = '';
		proposed = null;
		judging = true;
		try {
			for (let i = 0; i < 60 && slug === s; i++) {
				try {
					proposed = { ...(await cols.judgeRun(s)), kind: 'judge' };
					return;
				} catch (e) {
					if (!(e instanceof cols.HttpError && e.detail === 'judging')) {
						msg = e instanceof cols.HttpError && e.detail ? e.detail : 'failed';
						return;
					}
				}
				await new Promise((r) => setTimeout(r, 3000));
			}
			msg = 'judging';
		} finally {
			judging = false;
		}
	}
	async function judgeNow() {
		const plan = proposed;
		if (!plan || plan.kind !== 'judge') return;
		proposed = null;
		await act(() => cols.judgeApply(slug, plan));
	}

	// The server refuses both dry runs `anchors` while one waits; the buttons say so first.
	const anchorsQueued = $derived(!!d && d.books.some((b) => b.anchor_state === 'queued'));
	const judgeFailed = $derived(
		d ? Object.entries(d.judge_failures).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])) : []
	);

	// One query, with the history filter, and quiet: it leaves a message on the page alone,
	// and an answer that is late for another collection or filter is dropped.
	async function refresh() {
		const s = slug;
		const leaf = histLeaf;
		try {
			const x = await cols.detail(s, leaf);
			if (s === slug && leaf === histLeaf) take(x, { refreshed: true });
		} catch {
			// The next poll asks again.
		}
	}

	// While a re-sort, a filing or an anchor waits, keep the page current.
	const busy = $derived(
		!!d &&
			(d.resort?.status === 'queued' ||
				d.resort?.status === 'running' ||
				d.requests.some((r) => r.job === 'queued' || r.job === 'running') ||
				d.notes_queued > 0 ||
				anchorsQueued)
	);
	$effect(() => {
		if (!busy || !slug) return;
		const t = setInterval(() => {
			if (document.visibilityState === 'visible') void refresh();
		}, 3000);
		return () => clearInterval(t);
	});

	// Book 0 holds the leaves in no book; it is named by its title, never a numeral.
	const bk = (n: number) => (n === 0 ? (d?.loose_title ?? '') : roman(n));
	const place = (b: number | null, p: number | null) =>
		b === null ? '' : p === null ? bk(b) : `${bk(b)} ${p}`;
	// Every slot of a book, empty ones null, for a pin or move to go to. Book 0 has no cap,
	// so it draws only its leaves, each labelled with its number.
	const cells = (b: cols.AdminBook) => {
		if (b.number === 0) return b.leaves;
		const out: (cols.AdminLeaf | null)[] = Array(d?.leaves_per_book ?? 0).fill(null);
		for (const l of b.leaves) out[l.position - 1] = l;
		return out;
	};
	const pair = (x: [number, number] | null) => (x ? `${bk(x[0])} ${x[1]}` : '');
	const when = (s: string) => s.replace('T', ' ').slice(0, 16);
	// Rounded to three places, where a tiny negative fill would otherwise read -0.000.
	const n3 = (x: number | null | undefined) =>
		x === null || x === undefined ? '' : Math.abs(x) < 0.0005 ? '0.000' : x.toFixed(3);
	const rom = (n: number | null | undefined) => (n === null || n === undefined ? '' : bk(n));
	// A judged decision reads as the judge's whatever the row's reason, which is the action
	// that wrote it (a filing is `auto`, re-judging writes `resort`).
	const kind = (h: cols.HistoryRow) => (h.chosen_by === 'judge' ? 'judge' : h.reason);
	const said = (j: cols.JudgeSaid) =>
		j.error ? `judge ${j.error}` : `judge ${rom(j.book)}, runner-up ${rom(j.runner_up)}`;
</script>

<svelte:window onbeforeunload={warn} />

{#snippet mark(on: 'public' | 'filing only')}
	<span class="mark">{on}</span>
{/snippet}

<!-- Beside a section's save: that it differs from the server, and why its last save failed. -->
{#snippet status(k: Key)}
	{#if dirty.has(k)}<span class="unsaved">unsaved</span>{/if}
	{#if errs[k]}<span class="hot err">{errs[k]}</span>{/if}
{/snippet}

{#snippet icon(path: string)}
	<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
		<path d={path} fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="square" />
	</svg>
{/snippet}

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
	<!-- Every dirty section at once; always drawn, so the page does not shift when one is edited. -->
	<div class="bar">
		<button class="go" disabled={!dirtyKeys.length || saving} onclick={saveAll}>save all</button>
		{#if dirtyKeys.length}<span class="unsaved">unsaved {dirtyKeys.length}</span>{/if}
	</div>

	<!-- The collection's own text, all of it public. -->
	<section class="head">
		<label>
			<span class="l">title {@render mark('public')}</span>
			<input bind:value={fields.title} />
		</label>
		<div class="pair">
			<label>
				<span class="l">abbreviation {@render mark('public')}</span>
				<input bind:value={fields.abbrev} />
			</label>
			<label>
				<span class="l">latin {@render mark('public')}</span>
				<input bind:value={fields.abbrev_latin} />
			</label>
		</div>
		<label>
			<span class="l">gloss {@render mark('public')}</span>
			<Grow min={1} bind:value={fields.gloss} />
		</label>
		<label>
			<span class="l">preamble {@render mark('public')}</span>
			<Grow min={5} bind:value={fields.preamble} />
		</label>
		<label>
			<span class="l">vendor's description {@render mark('public')}</span>
			<Grow min={3} bind:value={fields.vendor_text} />
		</label>
		<div class="row">
			<button class="go" disabled={!dirty.has('fields') || saving} onclick={() => saveOne('fields')}>save</button>
			{@render status('fields')}
			<button class:link={d.published} onclick={flipPublished}>
				{d.published ? 'unpublish' : 'publish'}
			</button>
			<span class="dim">{d.published ? 'published' : 'not published'}</span>
			<a href="/collections/{d.slug}" class="dim">view</a>
		</div>
	</section>

	<!-- The record: rows to edit, and the page's own markup showing what they make. -->
	<section class="recsec">
		<div class="record">
			<span class="l">record {@render mark('public')}</span>
			{#each record as row, i (i)}
				<div class="rec">
					<input
						class="lab"
						id="rec-label-{i}"
						bind:value={row.label}
						aria-label="label {i + 1}"
						placeholder="label"
					/>
					<Grow
						min={1}
						id="rec-value-{i}"
						bind:value={row.value}
						aria-label="value {i + 1}"
						placeholder="value, one line per line"
					/>
					<div class="recacts">
						<button class="icon" aria-label="up" title="up" disabled={i === 0} onclick={() => shift(i, -1)}>
							{@render icon('M3 10.5 8 5.5l5 5')}
						</button>
						<button
							class="icon"
							aria-label="down"
							title="down"
							disabled={i === record.length - 1}
							onclick={() => shift(i, 1)}
						>
							{@render icon('M3 5.5 8 10.5l5-5')}
						</button>
						<button class="icon" aria-label="remove" title="remove" onclick={() => dropRow(i)}>
							{@render icon('M4 4l8 8M12 4l-8 8')}
						</button>
					</div>
				</div>
			{/each}
			<div class="row"><button class="link" onclick={addRow}>add row</button></div>
		</div>
		<div class="row">
			<button class="go" disabled={!dirty.has('record') || saving} onclick={() => saveOne('record')}>save</button>
			{@render status('record')}
		</div>
		<div class="preview">
			<span class="l dim">preview</span>
			{#if record.some((r) => r.label.trim() || r.value.trim())}
				<RecordList record={record.map((r) => [r.label, r.value])} onedit={editRow} />
			{:else}
				<p class="dim">0 rows</p>
			{/if}
		</div>
	</section>

	<!-- The filer's weights. -->
	{#if tuning}
		<h2>tuning {@render mark('filing only')}</h2>
		<div class="tuning">
			{#each WEIGHTS as [k, name, line] (k)}
				<label class="weight">
					<span class="l">{name}</span>
					<input
						type="number"
						step="any"
						min={d.tuning_limits[k][0]}
						max={d.tuning_limits[k][1]}
						bind:value={tuning[k]}
					/>
					<span class="line">{line}</span>
				</label>
			{/each}
			<label class="weight">
				<span class="tick">
					<input type="checkbox" bind:checked={tuning.weekly_resort} />
					<span class="l">weekly re-sort</span>
				</span>
				<span class="line">re-sorts every book once a week</span>
			</label>
		</div>
		<div class="row">
			<button disabled={!dirty.has('tuning') || saving} onclick={() => saveOne('tuning')}>save</button>
			{@render status('tuning')}
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

	<!-- Re-sort and re-judging: a dry run shows its plan, and apply sends exactly that back. -->
	<h2>re-sort</h2>
	<div class="row">
		<button disabled={anchorsQueued} onclick={dry}>dry run</button>
		<button
			disabled={proposed?.kind !== 'resort' || d.resort?.status === 'queued' || d.resort?.status === 'running'}
			onclick={resortNow}>re-sort now</button
		>
		<button disabled={judging || anchorsQueued} onclick={judgeDry}>judge</button>
		<button disabled={proposed?.kind !== 'judge'} onclick={judgeNow}>apply</button>
		{#if anchorsQueued}<span class="dim">anchors queued</span>{/if}
		{#if judging}<span class="dim">judging</span>{/if}
		{#if d.resort}<span class="dim" class:hot={d.resort.status === 'failed'}>{d.resort.status}</span>{/if}
		{#if d.last_resort_at}<span class="dim">last {when(d.last_resort_at)}</span>{/if}
	</div>
	{#if judgeFailed.length}
		<p class="hot">
			judge failed {judgeFailed.reduce((n, [, k]) => n + k, 0)}: {judgeFailed
				.map(([e, k]) => `${e} ${k}`)
				.join(', ')}
		</p>
	{/if}
	{#if d.resort?.last_error && d.resort.status === 'failed'}<p class="hot err">{d.resort.last_error}</p>{/if}
	{#if msg}<p class="hot">{msg}</p>{/if}
	{#if proposed}
		<p class="dim">
			{proposed.kind === 'judge' ? 'judge' : 'dry run'}: {proposed.moves.length}
			{proposed.moves.length === 1 ? 'move' : 'moves'}
		</p>
		{#if proposed.moves.length}
			<div class="wide">
				<table class="moves">
					<thead>
						<tr>
							<th></th>
							<th>leaf</th>
							<th>from</th>
							<th>to</th>
							<th class="num">margin</th>
							{#if proposed.kind === 'judge'}<th>reason</th>{/if}
						</tr>
					</thead>
					<tbody>
						{#each proposed.moves as m (m.image_id)}
							<tr>
								<td>{#if m.thumb}<img class="thumb" src={m.thumb} alt="" loading="lazy" />{/if}</td>
								<td><a href="/admin/pictures/{m.image_id}">{m.slug ?? m.image_id}</a></td>
								<td class="dim">{pair(m.before)}</td>
								<!-- A move to another book reads brighter than a shift within one. -->
								<td class:dim={m.before[0] === m.after[0]}>{pair(m.after)}</td>
								<td class="num dim">{n3(m.margin)}</td>
								{#if proposed.kind === 'judge'}<td class="dim">{m.reason ?? ''}</td>{/if}
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
		{#if proposed.kind === 'judge' && proposed.held.length}
			<p class="dim">held {proposed.held.length}</p>
			<div class="wide">
				<table class="moves">
					<thead>
						<tr><th></th><th>leaf</th><th>at</th><th>judged</th><th>why</th></tr>
					</thead>
					<tbody>
						{#each proposed.held as h (h.image_id)}
							<tr>
								<td>{#if h.thumb}<img class="thumb" src={h.thumb} alt="" loading="lazy" />{/if}</td>
								<td><a href="/admin/pictures/{h.image_id}">{h.slug ?? h.image_id}</a></td>
								<td class="dim">{pair(h.before)}</td>
								<td>{rom(h.book)}</td>
								<td class="dim">{h.why}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	{/if}

	<h2>unfiled</h2>
	{#if d.requests.length || d.uploads.length}
		<ul class="plain">
			{#each d.requests as q (q.image_id)}
				<li class="row">
					{#if q.thumb}<img class="thumb" src={q.thumb} alt="" />{/if}
					<a href="/admin/pictures/{q.image_id}">{q.slug ?? q.image_id}</a>
					<span class="dim">{q.status}</span>
					{#if q.book !== null}<span>{place(q.book, q.position)}</span>{/if}
					{#if q.job}<span class="dim">{q.job}</span>{/if}
					{#if q.error}<span class="hot">{q.error}</span>{/if}
					<button onclick={() => act(() => cols.retry(slug, q.image_id))}>retry</button>
					<button class="link" onclick={() => unfile(q.image_id)}>drop</button>
				</li>
			{/each}
			{#each d.uploads as u (u.sha256)}
				<li class="row">
					{#if u.image_id !== null}
						<a href="/admin/pictures/{u.image_id}">{u.filename ?? u.stem}</a>
					{:else}
						<span>{u.filename ?? u.stem}</span>
					{/if}
					<span class="dim" class:hot={u.stage === 'failed' || u.stage === 'lost'}>{u.stage}</span>
					{#if u.error}<span class="hot">{u.error}</span>{/if}
					<span class="dim">{when(u.created_at)}</span>
					{#if u.stage === 'failed'}<a href="/admin/queue">queue</a>{/if}
				</li>
			{/each}
		</ul>
	{:else}
		<p class="dim">0 unfiled</p>
	{/if}

	<!-- Each book: its text for the filer, its anchor's state, and its leaves by position. -->
	{#each d.books as b (b.number)}
		<section class="book">
			<h2>
				{#if b.number === 0}
					<!-- Book 0, the leaves in no book: a title, no cap, and no anchor to file by. -->
					{b.title}
					<span class="dim">{b.leaves.length} {b.leaves.length === 1 ? 'leaf' : 'leaves'}</span>
				{:else}
					book {roman(b.number)}
					<span class="dim">{b.leaves.length} of {d.leaves_per_book}</span>
					{#if !b.anchor_text}
						<span class="dim">no anchor</span>
					{:else}
						<span class:dim={b.anchor_state !== 'stale'} class:hot={b.anchor_state === 'stale'}>
							{b.anchor_state}
						</span>
						{#if b.anchor_embedded_at}<span class="dim">{when(b.anchor_embedded_at)}</span>{/if}
					{/if}
				{/if}
			</h2>
			{#if books[b.number]}
				<label>
					<span class="l">title {@render mark('public')}</span>
					<input bind:value={books[b.number].title} />
				</label>
				{#if b.number !== 0}
					<label>
						<span class="l">anchor {@render mark('filing only')}</span>
						<Grow min={3} bind:value={books[b.number].anchor_text} />
					</label>
					<div class="hints">
						<span class="l">tag hints {@render mark('filing only')}</span>
						<div class="chips">
							{#each books[b.number].tag_hints as h (h)}
								{@const n = b.hint_leaves[h]}
								<span class="chip" class:dead={n === 0}>
									{h}
									{#if n !== undefined}<span class="count">{n}</span>{/if}
									<button class="x" aria-label="remove {h}" onclick={() => dropHint(b.number, h)}>x</button>
								</span>
							{/each}
							<input
								class="add"
								placeholder="add"
								aria-label="add tag hint to book {roman(b.number)}"
								bind:value={books[b.number].adding}
								onkeydown={(e) => hintKey(e, b.number)}
								onblur={() => addHints(b.number)}
							/>
						</div>
					</div>
				{/if}
				<div class="row">
					<button disabled={!dirty.has(dr.bookKey(b.number)) || saving} onclick={() => saveOne(dr.bookKey(b.number))}
						>save</button
					>
					{@render status(dr.bookKey(b.number))}
				</div>
			{/if}
			<div class="grid" style:--n={Math.min(d.leaves_per_book, 11)}>
				{#each cells(b) as l, i (i)}
					{#if l}
						<button
							class="cell"
							class:sel={picked?.leaf.image_id === l.image_id}
							class:pin={l.pinned}
							title="{l.position} {l.slug ?? ''}"
							aria-label="leaf {l.position} {l.slug ?? ''}"
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
						<button class="link" onclick={() => filterHist(picked!.leaf.slug ?? String(picked!.leaf.image_id))}
							>history</button
						>
					</div>
					<div class="row">
						<select aria-label="book" bind:value={moveBook}>
							{#each Array.from({ length: d.book_count }, (_, k) => k + 1) as n (n)}
								<option value={String(n)}>{roman(n)}</option>
							{/each}
							{#if d.loose_title !== null}<option value="0">{d.loose_title}</option>{/if}
						</select>
						<input
							class="num"
							type="number"
							min="1"
							max={moveBook === '0' ? undefined : d.leaves_per_book}
							placeholder="leaf"
							aria-label="leaf"
							bind:value={moveLeaf}
						/>
						<button onclick={move}>move</button>
						<button onclick={pinFlip}>{picked.leaf.pinned ? 'unpin' : 'pin'}</button>
						<button class="link" onclick={() => unfile(picked!.leaf.image_id)}>unfile</button>
					</div>
					{#if explained && explained.image_id === picked.leaf.image_id}
						{@const x = explained}
						<!-- Why it is here: every book now, the leaf left out of its own. -->
						{#if x.now}
							<div class="wide">
								<table class="scores">
									<thead>
										<tr>
											<th>book</th>
											<th class="num">cos</th>
											<th class="num">tag</th>
											<th class="num">fill</th>
											<th class="num">total</th>
										</tr>
									</thead>
									<tbody>
										{#each x.now.books as t (t.number)}
											<tr class:here={t.number === x.book}>
												<td>{roman(t.number)}</td>
												<td class="num">{n3(t.cos)}</td>
												<td class="num">{n3(t.tag)}</td>
												<td class="num">{n3(t.fill)}</td>
												<td class="num">{n3(t.total)}</td>
											</tr>
										{/each}
									</tbody>
								</table>
							</div>
							<div class="row facts">
								<span><span class="dim">best</span> {roman(x.now.best)}</span>
								<span><span class="dim">runner-up</span> {rom(x.now.runner_up)}</span>
								<span><span class="dim">margin</span> {n3(x.now.margin)}</span>
								<span><span class="dim">here</span> {n3(x.now.lead)}</span>
							</div>
						{:else}
							<p class="dim">no vector</p>
						{/if}
						{#if x.filed}
							<div class="row facts">
								<span class="dim">filed {when(x.filed.at)}</span>
								<span>{place(x.filed.book, x.filed.position)}</span>
								<span class="r-{x.filed.chosen_by === 'judge' ? 'judge' : x.filed.reason}"
									>{x.filed.chosen_by ? `by ${x.filed.chosen_by}` : x.filed.reason}</span
								>
								{#if x.filed.scores?.judge}<span class="dim">{said(x.filed.scores.judge)}</span>{/if}
								{#if x.filed.scores?.full}<span class="dim">{rom(x.filed.scores.full)} full</span>{/if}
								{#if x.judged && x.filed.chosen_by !== 'judge'}<span class="dim">judge since</span>{/if}
							</div>
							{#if x.filed.scores?.judge?.reason}<p class="why">{x.filed.scores.judge.reason}</p>{/if}
						{/if}
						{#if x.judgement && x.judgement.reason !== x.filed?.scores?.judge?.reason}
							<div class="row facts">
								<span class="dim">now {said(x.judgement)}</span>
							</div>
							{#if x.judgement.reason}<p class="why">{x.judgement.reason}</p>{/if}
						{/if}
					{/if}
				</div>
			{/if}
			<!-- Each leaf's note, folded away: a book can hold 77. -->
			{#if b.leaves.length}
				{@const queued = b.leaves.filter((l) => l.note_queued).length}
				<div class="row notesbar">
					<button class="link" aria-expanded={!!showNotes[b.number]} onclick={() => (showNotes[b.number] = !showNotes[b.number])}
						>notes {b.leaves.length}</button
					>
					{#if bookNotesDirty(b)}<span class="unsaved">unsaved</span>{/if}
					{#if queued}<span class="dim">{queued} queued</span>{/if}
				</div>
				{#if showNotes[b.number]}
					<ul class="plain notes">
						{#each b.leaves as l (l.image_id)}
							{@const k = dr.noteKey(l.image_id)}
							<li>
								<div class="row nhead">
									<span class="dim">leaf {l.position}</span>
									<a href="/admin/pictures/{l.image_id}">{l.slug ?? l.image_id}</a>
									{#if l.note_source}<span class="dim">{l.note_source}</span>{/if}
									{#if l.note_queued}<span class="dim">queued</span>{/if}
									<button disabled={!dirty.has(k) || saving} onclick={() => saveOne(k)}>save</button>
									<button
										class="link"
										disabled={l.note_source === 'owner' || l.note_queued}
										title={l.note_source === 'owner' ? "the note is the owner's" : 'write note'}
										onclick={() => writeNote(l.image_id)}>write note</button
									>
									{@render status(k)}
								</div>
								<Grow min={1} bind:value={notes[l.image_id]} aria-label="note for leaf {l.position}" />
							</li>
						{/each}
					</ul>
				{/if}
			{/if}
		</section>
	{/each}

	<h2>history</h2>
	<div class="row">
		<input
			class="leaf"
			placeholder="leaf"
			aria-label="leaf for history"
			bind:value={histInput}
			onkeydown={(e) => e.key === 'Enter' && filterHist(histInput)}
		/>
		<button onclick={() => filterHist(histInput)}>filter</button>
		{#if histLeaf}<button class="link" onclick={() => filterHist('')}>all</button>{/if}
	</div>
	{#if hist.length}
		<div class="wide">
			<table class="history">
				<tbody>
					{#each hist as h (h.id)}
						<tr>
							<td class="dim">{when(h.at)}</td>
							<td><a href="/admin/pictures/{h.image_id}">{h.slug ?? h.image_id}</a></td>
							<td class="dim">{place(h.from_book, h.from_position)}</td>
							<td>{h.to_book === null ? 'out' : place(h.to_book, h.to_position)}</td>
							<td class="r-{kind(h)}">{kind(h)}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
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
	/* Whether the visitor ever reads a field: quiet, beside its name. */
	.mark {
		margin-left: 0.5rem;
		font-size: 0.875rem;
		color: var(--ink-500);
	}
	h2 .mark {
		margin-left: 0.25rem;
	}
	.pair {
		display: flex;
		flex-wrap: wrap;
		gap: 0 1.5rem;
	}
	/* Every dirty section's save in one place, as high as the page's top while it scrolls. */
	.bar {
		position: sticky;
		top: 0;
		z-index: 2;
		display: flex;
		align-items: center;
		gap: 1.25rem;
		padding: 0.5rem 0;
		background: var(--background);
	}
	.unsaved {
		font-size: 0.875rem;
		color: var(--muted-foreground);
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
	.recacts {
		grid-column: 2;
		display: flex;
		gap: 0.25rem;
		margin-top: -0.25rem;
	}
	/* Small glyphs, but a thumb-sized place to press. */
	.recacts .icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 2.75rem;
		min-height: 2.75rem;
		color: var(--muted-foreground);
	}
	.recacts .icon:disabled {
		color: var(--ink-700);
	}
	.recacts .icon:hover:not(:disabled),
	.recacts .icon:focus-visible {
		color: var(--foreground);
	}
	@media (max-width: 30rem) {
		.rec {
			grid-template-columns: 1fr;
		}
		.recacts {
			grid-column: 1;
		}
	}
	.preview {
		margin-top: 1.5rem;
		padding-left: 1rem;
		border-left: 1px solid var(--ink-700);
		max-width: 40rem;
	}
	.preview .l {
		display: block;
		font-size: 0.875rem;
		margin-bottom: 0.5rem;
	}
	.preview p {
		margin: 0;
	}
	.notes {
		gap: 1rem;
		margin-top: 0.75rem;
		max-width: 40rem;
	}
	/* Its actions sit with its name, so a long list stays short; still a place to press. */
	.nhead {
		gap: 0 1rem;
		font-size: 0.875rem;
	}
	.nhead button {
		min-height: 2.5rem;
	}
	.notes li :global(textarea) {
		width: 100%;
	}
	.row {
		margin-top: 0.75rem;
	}
	.tuning {
		display: flex;
		flex-direction: column;
		max-width: 40rem;
	}
	.weight {
		display: grid;
		grid-template-columns: 9rem 7rem 1fr;
		align-items: center;
		gap: 0 1rem;
	}
	.weight .line {
		font-size: 0.875rem;
		color: var(--muted-foreground);
	}
	.weight .tick {
		grid-column: 1 / 3;
	}
	@media (max-width: 30rem) {
		.weight {
			grid-template-columns: 1fr 7rem;
		}
		.weight .line {
			grid-column: 1 / -1;
		}
	}
	.tick {
		display: flex;
		flex-direction: row;
		align-items: center;
		gap: 0.5rem;
		min-height: 44px;
	}
	.err {
		font-size: 0.875rem;
		overflow-wrap: anywhere;
	}
	.wide {
		overflow-x: auto;
		max-width: 100%;
	}
	/* As wide as their columns: the admin's tables are full width by default. */
	.wide table {
		margin-top: 0.75rem;
		width: auto;
	}
	.moves td {
		vertical-align: middle;
	}
	.moves .thumb {
		display: block;
		width: 2.5rem;
		height: 2.5rem;
	}
	.book h2 {
		display: flex;
		flex-wrap: wrap;
		gap: 0 1rem;
		align-items: baseline;
	}
	.hints {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
		margin-top: 0.75rem;
		max-width: 40rem;
	}
	.chips {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem;
	}
	.chip {
		display: inline-flex;
		align-items: baseline;
		gap: 0.4rem;
		padding: 0.2rem 0 0.2rem 0.6rem;
		border: 1px solid var(--ink-700);
	}
	.chip .count {
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
	}
	/* A hint no leaf carries weighs nothing yet. */
	.chip.dead {
		color: var(--ink-500);
		border-style: dashed;
	}
	.chip.dead .count {
		color: var(--ink-500);
	}
	.chip .x {
		color: var(--ink-500);
		padding: 0 0.6rem 0 0.2rem;
		min-width: 1.5rem;
	}
	.chips .add {
		width: 9rem;
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
		max-width: 40rem;
	}
	.scores tr {
		color: var(--muted-foreground);
	}
	.scores tr.here {
		color: var(--foreground);
	}
	.facts {
		font-size: 0.875rem;
	}
	.why {
		margin: 0.35rem 0 0;
		font-size: 0.875rem;
		color: var(--muted-foreground);
		max-width: 40rem;
	}
	.num {
		width: 5rem;
	}
	.leaf {
		width: 12rem;
	}
	td a {
		overflow-wrap: anywhere;
	}
	/* Who decided a move, each in its own colour: the owner in the pins' cyan, the judge
	   in lavender, a re-sort plain, the filer's own choice quiet. */
	.r-owner {
		color: var(--secondary);
	}
	.r-judge {
		color: var(--accent);
	}
	.r-resort {
		color: var(--foreground);
	}
	.r-auto {
		color: var(--muted-foreground);
	}
	/* Paper is white: the palette turns to ink, and every field prints whole. */
	@media print {
		:global(.admin) {
			--foreground: #000;
			--muted-foreground: #555;
			--background: #fff;
			--muted: #eee;
			--ink-400: #777;
			--ink-500: #888;
			--ink-700: #bbb;
			--ink-800: #ccc;
			--ink-900: #fff;
			--primary: #b0005a;
			--secondary: #006b85;
			--accent: #5b3a9e;
			color: #000;
		}
		.wide {
			overflow: visible;
		}
		/* The preview repeats the record, and the bar and pencils are for the screen. */
		.bar,
		.preview,
		.recacts {
			display: none;
		}
	}
</style>
