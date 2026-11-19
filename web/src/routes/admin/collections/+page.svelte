<script lang="ts">
	import { onMount } from 'svelte';
	import * as col from '../../../lib/admin-collections';
	import { roman } from '../../../lib/collections';

	let all = $state<col.AdminCollection[]>([]);
	let tuning = $state<col.Tuning>({});
	let loaded = $state(false);
	let msg = $state('');
	let pick = $state(0);
	const c = $derived(all[pick] ?? null);

	// Drafts keyed by what they edit, filled from the server only where untouched.
	let head = $state({ title: '', preamble: '' });
	let books = $state<Record<number, { title: string; anchor: string; hints: string }>>({});
	let tunes = $state<Record<string, string>>({});
	let seenHead = { title: '', preamble: '' };
	let seenBooks: Record<number, { title: string; anchor: string; hints: string }> = {};

	let selected = $state<{ book: number; leaf: col.AdminLeaf } | null>(null);
	let to = $state({ book: '', leaf: '' });
	let proposal = $state<col.Move[] | null>(null);

	async function load() {
		try {
			const r = await col.list();
			all = r.collections;
			tuning = r.tuning;
			const x = all[pick];
			if (x) {
				const h = { title: x.title, preamble: x.preamble };
				if (head.title === seenHead.title) head.title = h.title;
				if (head.preamble === seenHead.preamble) head.preamble = h.preamble;
				seenHead = h;
				const next: typeof books = {};
				for (const b of x.books) {
					const s = { title: b.title ?? '', anchor: b.anchor_text, hints: b.tag_hints.join(', ') };
					const d = books[b.number];
					const was = seenBooks[b.number];
					next[b.number] =
						d && was
							? {
									title: d.title === was.title ? s.title : d.title,
									anchor: d.anchor === was.anchor ? s.anchor : d.anchor,
									hints: d.hints === was.hints ? s.hints : d.hints
								}
							: s;
					seenBooks[b.number] = s;
				}
				books = next;
				if (selected) {
					const lf = x.books.flatMap((b) => b.leaves.map((l) => ({ book: b.number, leaf: l })));
					selected = lf.find((l) => l.leaf.image_id === selected!.leaf.image_id) ?? null;
				}
			}
			for (const [k, v] of Object.entries(tuning)) if (!(k in tunes)) tunes[k] = String(v.value);
		} catch {
			msg = 'failed';
		}
		loaded = true;
	}

	onMount(load);

	async function act(fn: () => Promise<unknown>) {
		msg = '';
		try {
			await fn();
		} catch (e) {
			msg = e instanceof col.CollectionError ? e.message : 'failed';
		}
		await load();
	}

	const slots = (b: col.AdminBook, n: number) => {
		const out: (col.AdminLeaf | null)[] = Array(n).fill(null);
		for (const l of b.leaves) out[l.position - 1] = l;
		return out;
	};
	const where = (p: col.Pos | null) => (p ? `${roman(p.book)}, ${p.position}` : '--');
	const hints = (s: string) =>
		s
			.split(/[,\n]/)
			.map((h) => h.trim())
			.filter(Boolean);
	const tuneKeys = ['collections.k', 'collections.alpha', 'collections.beta', 'collections.margin'];

	function choose(book: number, leaf: col.AdminLeaf) {
		selected = selected?.leaf.image_id === leaf.image_id ? null : { book, leaf };
		to = { book: '', leaf: '' };
	}
</script>

{#if loaded && !c}
	<p class="dim">0 collections</p>
{:else if c}
	{#if all.length > 1}
		<p class="row">
			{#each all as x, i (x.slug)}
				<button class="link" class:on={i === pick} onclick={() => ((pick = i), load())}>{x.slug}</button>
			{/each}
		</p>
	{/if}

	<section class="form">
		<label><span class="l">title</span><input bind:value={head.title} /></label>
		<label><span class="l">preamble</span><textarea rows="5" bind:value={head.preamble}></textarea></label>
		<div class="row">
			<button
				disabled={head.title === c.title && head.preamble === c.preamble}
				onclick={() => act(() => col.patch(c.slug, { title: head.title, preamble: head.preamble }))}
				>save</button
			>
			<label class="check">
				<input
					type="checkbox"
					checked={c.published}
					onchange={(e) => act(() => col.patch(c.slug, { published: e.currentTarget.checked }))}
				/>
				published
			</label>
		</div>
	</section>

	<section>
		<h2>re-sort</h2>
		<div class="row">
			<button onclick={() => act(async () => (proposal = (await col.resort(c.slug, true)).moves))}>preview</button>
			<button
				disabled={proposal === null}
				onclick={() =>
					act(async () => {
						await col.resort(c.slug, false);
						proposal = null;
					})}>re-sort now</button
			>
			<span class="dim">{c.resorted_at ? `last ${c.resorted_at.slice(0, 16).replace('T', ' ')}` : ''}</span>
		</div>
		{#if proposal}
			{#if proposal.length}
				<ul class="plain moves">
					{#each proposal as m (m.image_id)}
						<li>
							<span>{m.slug ?? m.image_id}</span>
							<span class="dim">{where(m.from)} to {where(m.to)}</span>
							{#if m.from.book !== m.to.book}<span class="hot">book</span>{/if}
						</li>
					{/each}
				</ul>
			{:else}
				<p class="dim">0 moves</p>
			{/if}
		{/if}
	</section>

	{#if c.requests.length}
		<section>
			<h2>unfiled</h2>
			<ul class="plain">
				{#each c.requests as r (r.image_id)}
					<li class="row">
						{#if r.thumb}<img class="t" src={r.thumb} alt="" />{/if}
						<a href="/admin/pictures/{r.image_id}">{r.slug ?? r.image_id}</a>
						<span class="dim">{r.status}</span>
						{#if r.book}<span class="dim">{roman(r.book)}{r.position ? `, ${r.position}` : ''}</span>{/if}
						{#if r.error}<span class="hot">{r.error}</span>{/if}
						<button onclick={() => act(() => col.retry(c.slug, r.image_id))}>retry</button>
						<button class="link" onclick={() => act(() => col.unfile(c.slug, r.image_id))}>drop</button>
					</li>
				{/each}
			</ul>
		</section>
	{/if}

	{#if msg}<p class="hot">{msg}</p>{/if}

	{#each c.books as b (b.number)}
		{@const d = books[b.number]}
		<section class="book">
			<h2>book {roman(b.number)} <span class="dim">{b.leaves.length} / {c.leaves_per_book}</span></h2>
			<div class="cells" style:--n={c.leaves_per_book}>
				{#each slots(b, c.leaves_per_book) as s, i (i)}
					{#if s}
						<button
							class="cell"
							class:pinned={s.pinned_position}
							class:bound={s.pinned_book && !s.pinned_position}
							class:sel={selected?.leaf.image_id === s.image_id}
							onclick={() => choose(b.number, s)}
							aria-label="{s.slug ?? s.image_id}, leaf {s.position}"
						>
							{#if s.thumb}<img src={s.thumb} alt="" loading="lazy" />{/if}
						</button>
					{:else}
						<span class="cell empty"></span>
					{/if}
				{/each}
			</div>

			{#if selected && selected.book === b.number}
				{@const lf = selected.leaf}
				<div class="row sel-row">
					<a href="/admin/pictures/{lf.image_id}">{lf.slug ?? lf.image_id}</a>
					<span class="dim">
						leaf {lf.position}{lf.pinned_position ? ', pinned' : lf.pinned_book ? ', pinned to book' : ''}
					</span>
					<button onclick={() => act(() => col.pin(c.slug, lf.image_id, !lf.pinned_book))}>
						{lf.pinned_book ? 'unpin' : 'pin'}
					</button>
					<input class="num" placeholder="book" aria-label="book" bind:value={to.book} />
					<input class="num" placeholder="leaf" aria-label="leaf" bind:value={to.leaf} />
					<button disabled={!to.book.trim()} onclick={() => act(() => col.place(c.slug, lf.image_id, to.book, to.leaf))}
						>move</button
					>
					<button class="link" onclick={() => act(() => col.unfile(c.slug, lf.image_id))}>unfile</button>
				</div>
			{/if}

			{#if d}
				<div class="form">
					<label><span class="l">title</span><input bind:value={d.title} /></label>
					<label>
						<span class="l">anchor <span class="dim">{b.anchored ? '' : 'not embedded'}</span></span>
						<textarea rows="3" bind:value={d.anchor}></textarea>
					</label>
					<label><span class="l">hints</span><input bind:value={d.hints} /></label>
					<div class="row">
						<button
							disabled={d.title === (b.title ?? '') && d.anchor === b.anchor_text && d.hints === b.tag_hints.join(', ')}
							onclick={() =>
								act(() =>
									col.patchBook(c.slug, b.number, {
										title: d.title,
										anchor_text: d.anchor,
										tag_hints: hints(d.hints)
									})
								)}>save</button
						>
					</div>
				</div>
			{/if}
		</section>
	{/each}

	<section class="form">
		<h2>tuning</h2>
		{#each tuneKeys as k (k)}
			<label class="row">
				<span class="l">{k.replace('collections.', '')}</span>
				<input class="num" bind:value={tunes[k]} />
				<button
					disabled={!tuning[k] || tunes[k] === String(tuning[k].value)}
					onclick={() => act(() => col.tune(k, Number(tunes[k])))}>save</button
				>
				<span class="dim">{tuning[k]?.default}</span>
			</label>
		{/each}
		<label class="check">
			<input
				type="checkbox"
				checked={Boolean(tuning['collections.weekly_resort']?.value)}
				onchange={(e) => act(() => col.tune('collections.weekly_resort', e.currentTarget.checked))}
			/>
			weekly re-sort
		</label>
	</section>

	<section>
		<h2>history</h2>
		{#if c.history.length}
			<ul class="plain moves">
				{#each c.history as h, i (i)}
					<li>
						<span>{h.slug ?? h.image_id}</span>
						<span class="dim">{where(h.from)} to {where(h.to)}</span>
						<span class="dim">{h.reason}</span>
						<span class="dim">{h.at.slice(0, 16).replace('T', ' ')}</span>
					</li>
				{/each}
			</ul>
		{:else}
			<p class="dim">0 moves</p>
		{/if}
	</section>
{/if}

<style>
	section {
		margin-bottom: 2.5rem;
	}
	.form {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
		max-width: 40rem;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}
	label.row,
	label.check {
		flex-direction: row;
		align-items: center;
		gap: 0.75rem;
	}
	.l {
		font-size: 0.85rem;
		color: var(--muted-foreground);
	}
	.row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem 1rem;
	}
	h2 {
		font-size: 1rem;
		font-weight: 500;
		margin: 0 0 0.75rem;
	}
	.dim {
		color: var(--muted-foreground);
	}
	.hot {
		color: var(--primary);
	}
	.on {
		color: var(--foreground);
	}
	.num {
		width: 4rem;
	}
	.t {
		width: 2rem;
		height: 2rem;
		object-fit: cover;
	}
	.moves li {
		display: flex;
		flex-wrap: wrap;
		gap: 0 1rem;
	}
	.cells {
		display: grid;
		grid-template-columns: repeat(var(--n), minmax(0, 1fr));
		gap: 1px;
		margin-bottom: 0.75rem;
	}
	@media (max-width: 48rem) {
		.cells {
			grid-template-columns: repeat(11, minmax(0, 1fr));
		}
	}
	.cell {
		display: block;
		aspect-ratio: 1;
		padding: 0;
		border: 0;
		background: var(--ink-900);
		overflow: hidden;
		cursor: pointer;
	}
	.cell img {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.empty {
		background: transparent;
		box-shadow: inset 0 0 0 1px var(--ink-800);
		cursor: default;
	}
	.pinned {
		box-shadow: 0 0 0 1px var(--secondary);
	}
	.bound {
		box-shadow: 0 0 0 1px var(--ink-500);
	}
	.sel {
		outline: 2px solid var(--primary);
		outline-offset: 1px;
	}
	.sel-row {
		margin-bottom: 1rem;
	}
</style>
