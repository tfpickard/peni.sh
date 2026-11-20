<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import Picture from '../../../../lib/components/Picture.svelte';
	import Cropper from '../../../../lib/components/admin/Cropper.svelte';
	import * as pics from '../../../../lib/admin-pictures';
	import * as cols from '../../../../lib/admin-collections';
	import { roman } from '../../../../lib/roman';

	let { data } = $props();
	const id = $derived(data.id);

	let d = $state<pics.Detail | null>(null);
	let missing = $state(false);
	let msg = $state('');
	let cropping = $state(false);
	let confirming = $state(false);
	let typed = $state('');
	// Book and leaf for each collection's file or move, blank meaning the filer picks.
	// Seeded by load, one per collection, so the fields can bind to them.
	let filing = $state<Record<string, { book: string; leaf: string }>>({});
	// The conservator note per collection, kept like a draft: a poll only replaces it
	// while it still matches what the server last said.
	let notes = $state<Record<string, string>>({});
	let seenNotes: Record<string, string> = {};

	type Draft = {
		caption: string;
		description: string;
		analysis: string;
		prompt: string;
		tags: string;
		slug: string;
	};
	const blank: Draft = { caption: '', description: '', analysis: '', prompt: '', tags: '', slug: '' };
	let draft = $state<Draft>({ ...blank });
	// The override's checkboxes, kept like a draft: a poll only resets them if untouched.
	let picks = $state<string[]>([]);
	let seenPicks = '';
	// What the server last said, so a poll only replaces drafts the owner has not touched.
	let seen: Draft = { ...blank };

	const serverValues = (x: pics.Detail): Draft => ({
		caption: x.caption?.body ?? '',
		description: x.description?.body ?? '',
		analysis: x.analysis?.body ?? '',
		prompt: x.prompt?.body ?? '',
		tags: x.tags.map((t) => t.tag).join('\n'),
		slug: x.slug
	});

	async function load() {
		try {
			const x = await pics.detail(id);
			const next = serverValues(x);
			for (const k of Object.keys(next) as (keyof Draft)[]) {
				if (draft[k] === seen[k]) draft[k] = next[k];
			}
			seen = next;
			const kinds = x.nudity?.kinds ?? [];
			if (picks.join(',') === seenPicks) picks = [...kinds];
			seenPicks = kinds.join(',');
			for (const c of x.collections) {
				filing[c.slug] ??= { book: '', leaf: '' };
				const n = c.leaf?.note ?? '';
				if ((notes[c.slug] ?? '') === (seenNotes[c.slug] ?? '')) notes[c.slug] = n;
				seenNotes[c.slug] = n;
			}
			d = x;
		} catch (e) {
			if (!d) missing = true;
			else if (!(e instanceof pics.HttpError)) msg = 'failed';
		}
	}

	onMount(load);

	const running = $derived(d ? pics.active(d.jobs) : false);
	const unpublished = $derived(d?.status === 'unpublished');
	// Either direction waiting or running: the status shown is about to change.
	const moving = $derived(
		d ? pics.active(d.jobs.filter((j) => j.type === 'unpublish' || j.type === 'republish')) : false
	);
	$effect(() => {
		if (!running) return;
		const t = setInterval(load, 2000);
		return () => clearInterval(t);
	});

	const label = (t: pics.Text | null) => (!t ? '' : t.source === 'owner' ? 'owner' : (t.model ?? t.source));
	const tagSource = $derived.by(() => {
		const s = new Set(
			(d?.tags ?? []).map((t) => (t.source === 'model' ? (t.model ?? t.source) : t.source))
		);
		return [...s].join(' ');
	});

	async function act(fn: () => Promise<unknown>) {
		msg = '';
		try {
			await fn();
		} catch (e) {
			msg = e instanceof pics.HttpError && e.status === 409 ? 'taken' : 'failed';
		}
		await load();
	}

	function save(f: pics.Field) {
		const v = f === 'tags' ? draft.tags.split('\n').map((s) => s.trim()).filter(Boolean) : draft[f].trim();
		return act(() => pics.patch(id, { [f]: v }));
	}
	const regen = (...f: pics.Regen[]) => act(() => pics.regen(id, f));
	const detect = () => act(() => pics.detectNudity(id));
	function toggle(k: string) {
		const on = new Set(picks);
		if (on.has(k)) on.delete(k);
		else on.add(k);
		picks = (d?.nudity_kinds ?? []).filter((x) => on.has(x));
	}
	// Saving an unchanged choice still means something when the detector made it: it
	// turns the detector's answer into the owner's, which no rerun will overwrite.
	const picksDirty = $derived(
		!d ? false : d.nudity?.source !== 'owner' || picks.join(',') !== seenPicks
	);
	const saveNudity = () => act(() => pics.setNudity(id, picks));
	const nuditySource = $derived(
		d?.nudity?.source === 'owner' ? 'owner' : (d?.nudity?.model ?? '')
	);
	const nudity = $derived(
		!d?.nudity ? 'unchecked' : d.nudity.kinds.length ? d.nudity.kinds.join(', ') : 'none'
	);
	const flip = () => act(() => (unpublished ? pics.republish(id) : pics.unpublish(id)));

	async function applyCrop(r: pics.Rect | null) {
		cropping = false;
		await act(() => pics.crop(id, r));
	}

	async function del() {
		if (!d || typed !== d.slug) return;
		try {
			await pics.remove(id);
			await goto('/admin/pictures');
		} catch {
			msg = 'failed';
		}
	}

	const fields = (slug: string) => filing[slug] ?? { book: '', leaf: '' };
	async function collect(fn: () => Promise<unknown>) {
		msg = '';
		try {
			await fn();
		} catch (e) {
			msg = e instanceof cols.HttpError && e.detail ? e.detail : 'failed';
		}
		await load();
	}
	function member(c: pics.PictureCollection) {
		if (c.leaf || c.request) return collect(() => cols.unfile(c.slug, id));
		const f = fields(c.slug);
		return collect(() =>
			cols.place(c.slug, id, f.book ? Number(f.book) : null, f.book && f.leaf ? Number(f.leaf) : null)
		);
	}
	function moveLeaf(c: pics.PictureCollection) {
		const f = fields(c.slug);
		if (!f.book) return;
		return collect(() => cols.place(c.slug, id, Number(f.book), f.leaf ? Number(f.leaf) : null));
	}
	const noteDirty = (c: pics.PictureCollection) => (notes[c.slug] ?? '') !== (c.leaf?.note ?? '');
	const saveNote = (c: pics.PictureCollection) =>
		collect(() => cols.setNote(c.slug, id, notes[c.slug] ?? ''));
	// Only offered on an empty field. An owner note emptied here is cleared first, since
	// the conservator never writes over the owner's.
	const askNote = (c: pics.PictureCollection) =>
		collect(async () => {
			if (c.leaf?.note_source === 'owner') await cols.setNote(c.slug, id, '');
			await cols.writeNote(c.slug, id);
		});
	const noteBy = (c: pics.PictureCollection) =>
		!c.leaf?.note ? '' : c.leaf.note_source === 'owner' ? 'owner' : (c.leaf.note_model ?? '');
	const where = (c: pics.PictureCollection) => {
		if (c.leaf) {
			const pin = c.leaf.pinned ? 'pinned' : c.leaf.book_pinned ? 'book pinned' : 'auto';
			return `book ${roman(c.leaf.book)} leaf ${c.leaf.position} / ${pin}`;
		}
		if (c.request) {
			const want = c.request.book ? ` ${roman(c.request.book)}${c.request.position ? ` ${c.request.position}` : ''}` : '';
			return `unfiled${want}${c.request.error ? ` / ${c.request.error}` : ''}`;
		}
		return '';
	};

	const item = $derived(
		d ? { slug: d.slug, width: d.width, height: d.height, blurhash: '', palette: [], variants: d.variants } : null
	);
	const dirty = (k: keyof Draft) => draft[k] !== seen[k];
</script>

<svelte:head>
	<title>{d?.slug ?? 'picture'} -- peni.sh</title>
	<meta name="robots" content="noindex" />
</svelte:head>

<p class="top"><a href="/admin/pictures">pictures</a></p>

{#if missing}
	<p class="dim">not here</p>
{:else if d && item}
	<div class="grid" class:wide={cropping}>
		<div class="pic">
			{#if cropping}
				<Cropper
					src={pics.originalUrl(id)}
					initial={d.crop}
					onapply={applyCrop}
					onreset={() => applyCrop(null)}
					oncancel={() => (cropping = false)}
				/>
			{:else}
				{#if unpublished}
					<!-- Its derivatives are held where nothing serves them. -->
					<img class="orig" src={pics.originalUrl(id)} alt="" />
				{:else}
					<Picture image={item} sizes="(min-width: 48rem) 20rem, 100vw" eager />
				{/if}
				<div class="row">
					<button onclick={() => (cropping = true)}>crop</button>
					{#if d.status === 'live'}<a href="/i/{d.slug}">view</a>{/if}
					{#if d.status === 'live' || unpublished}
						<button disabled={moving} onclick={flip}>{unpublished ? 'republish' : 'unpublish'}</button>
					{/if}
					<span class="dim">{d.crop ? `cropped ${d.crop.w} x ${d.crop.h}` : ''}</span>
				</div>
			{/if}
			<p class="dim">
				{d.status} / {d.votes.up} up {d.votes.down} down / {d.comments.pending} pending
				{d.comments.approved} approved {d.comments.rejected} rejected
			</p>
			{#if d.purge?.length}
				<h2>purge</h2>
				<pre class="purge">{d.purge.join('\n')}</pre>
			{/if}
		</div>

		<div class="form">
			<label>
				<span class="l">caption <span class="dim">{label(d.caption)}</span></span>
				<input bind:value={draft.caption} />
			</label>
			<div class="row">
				<button disabled={!dirty('caption')} onclick={() => save('caption')}>save</button>
				<button onclick={() => regen('caption')}>regenerate</button>
			</div>

			<label>
				<span class="l">description <span class="dim">{label(d.description)}</span></span>
				<textarea rows="6" bind:value={draft.description}></textarea>
			</label>
			<div class="row">
				<button disabled={!dirty('description')} onclick={() => save('description')}>save</button>
				<button onclick={() => regen('description')}>regenerate</button>
			</div>

			<label>
				<span class="l">analysis <span class="dim">{label(d.analysis)}</span></span>
				<textarea rows="8" bind:value={draft.analysis}></textarea>
			</label>
			<div class="row">
				<button disabled={!dirty('analysis')} onclick={() => save('analysis')}>save</button>
				<button onclick={() => regen('analysis')}>regenerate</button>
			</div>

			<label>
				<span class="l">prompt <span class="dim">{label(d.prompt)}</span></span>
				<textarea rows="3" bind:value={draft.prompt}></textarea>
			</label>
			<div class="row">
				<button disabled={!dirty('prompt')} onclick={() => save('prompt')}>save</button>
			</div>

			<label>
				<span class="l">tags <span class="dim">{tagSource}</span></span>
				<textarea rows="5" bind:value={draft.tags}></textarea>
			</label>
			<div class="row">
				<button disabled={!dirty('tags')} onclick={() => save('tags')}>save</button>
				<button onclick={() => regen('tags')}>regenerate</button>
			</div>

			<label>
				<span class="l">slug</span>
				<input bind:value={draft.slug} />
			</label>
			<div class="row">
				<button disabled={!dirty('slug')} onclick={() => save('slug')}>save</button>
			</div>

			<div class="nude">
				<span class="l">nudity <span class="dim">{nuditySource}</span></span>
				<p class="nudity">{nudity}</p>
				{#if d.nudity?.seen}<p class="dim nudity">{d.nudity.seen}</p>{/if}
				<div class="kinds">
					{#each d.nudity_kinds as k (k)}
						<label class="kind">
							<input type="checkbox" checked={picks.includes(k)} onchange={() => toggle(k)} />
							{k}
						</label>
					{/each}
				</div>
			</div>
			<div class="row">
				<button disabled={!picksDirty} onclick={saveNudity}>save</button>
				<button onclick={detect}>detect nudity</button>
			</div>

			{#each d.collections as c (c.slug)}
				<div class="nude">
					<label class="kind">
						<input type="checkbox" checked={!!(c.leaf || c.request)} onchange={() => member(c)} />
						{c.slug}
						<span class="dim">{where(c)}</span>
					</label>
					<div class="row">
						<select aria-label="book" bind:value={filing[c.slug].book}>
							<option value="">book</option>
							{#each Array.from({ length: c.book_count }, (_, k) => k + 1) as n (n)}
								<option value={String(n)}>{roman(n)}</option>
							{/each}
						</select>
						<input
							class="num"
							type="number"
							min="1"
							max={c.leaves_per_book}
							placeholder="leaf"
							aria-label="leaf"
							bind:value={filing[c.slug].leaf}
							disabled={!filing[c.slug]?.book}
						/>
						{#if c.leaf}
							<button disabled={!filing[c.slug]?.book} onclick={() => moveLeaf(c)}>move</button>
						{/if}
						<a class="dim" href="/admin/collections">{c.abbrev_latin.toLowerCase()}</a>
					</div>
					{#if c.leaf}
						<label>
							<span class="l">conservator note <span class="dim">{noteBy(c)}</span></span>
							<textarea rows="4" bind:value={notes[c.slug]}></textarea>
						</label>
						<div class="row">
							<button disabled={!noteDirty(c)} onclick={() => saveNote(c)}>save</button>
							{#if !(notes[c.slug] ?? '').trim()}
								<button onclick={() => askNote(c)}>write note</button>
							{/if}
						</div>
					{/if}
				</div>
			{/each}

			<div class="row all">
				<button onclick={() => regen('caption', 'description', 'analysis', 'tags')}>regenerate all</button>
				<button onclick={() => regen('haiku')}>regenerate haiku</button>
				<button onclick={() => regen('embedding')}>re-embed</button>
			</div>

			{#if msg}<p class="bad">{msg}</p>{/if}

			<h2>jobs</h2>
			{#if d.jobs.length}
				<ul class="jobs">
					{#each d.jobs as j (j.id)}
						<li>
							<span>{j.type}</span>
							<span class="dim" class:bad={j.status === 'failed'}>{j.status}</span>
							{#if j.last_error}<span class="err">{j.last_error}</span>{/if}
						</li>
					{/each}
				</ul>
			{:else}
				<p class="dim">0 jobs</p>
			{/if}
			<p class="logs"><a href="/admin/logs?image={id}">logs</a></p>

			<div class="del">
				{#if confirming}
					<input placeholder={d.slug} aria-label="slug" bind:value={typed} />
					<button disabled={typed !== d.slug} onclick={del}>delete</button>
					<button class="link" onclick={() => ((confirming = false), (typed = ''))}>cancel</button>
				{:else}
					<button class="link" onclick={() => (confirming = true)}>delete</button>
				{/if}
			</div>
		</div>
	</div>
{/if}

<style>
	.top {
		margin: 0 0 1.5rem;
	}
	.grid {
		display: grid;
		gap: 2rem;
	}
	@media (min-width: 48rem) {
		.grid {
			grid-template-columns: 20rem 1fr;
			align-items: start;
		}
		.grid.wide {
			grid-template-columns: 1fr;
		}
		.pic {
			position: sticky;
			top: 1rem;
		}
		.wide .pic {
			position: static;
		}
	}
	.pic {
		min-width: 0;
	}
	.orig {
		display: block;
		width: 100%;
		height: auto;
	}
	.purge {
		margin: 0;
		font: inherit;
		font-size: 0.875rem;
		color: var(--muted-foreground);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		user-select: all;
	}
	/* The cropper needs the room; the grid column is only for the still image. */
	.pic :global(.crop) {
		width: min(100%, 52rem);
	}
	.form {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		min-width: 0;
	}
	label {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
		margin-top: 0.75rem;
	}
	.l {
		color: var(--foreground);
	}
	.dim {
		color: var(--muted-foreground);
		font-size: 0.875rem;
	}
	p.dim {
		margin: 0.75rem 0 0;
	}
	.nude {
		margin-top: 0.75rem;
	}
	p.nudity {
		margin: 0.25rem 0 0;
	}
	.kinds {
		display: flex;
		flex-wrap: wrap;
		gap: 0.25rem 1.25rem;
		margin-top: 0.5rem;
	}
	label.kind {
		flex-direction: row;
		align-items: center;
		gap: 0.4rem;
		min-height: 44px;
		margin-top: 0;
		font-size: 0.875rem;
		color: var(--muted-foreground);
	}
	label.kind input {
		width: 1rem;
		height: 1rem;
		margin: 0;
		padding: 0;
		border: 0;
		accent-color: var(--primary);
	}
	.num {
		width: 5rem;
	}
	.row {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem 1.5rem;
		align-items: center;
		margin-top: 0.5rem;
	}
	.all {
		margin-top: 1.5rem;
	}
	button {
		background: transparent;
		color: var(--primary);
		border: 0;
		font: inherit;
		padding: 0.25rem 0;
		cursor: pointer;
	}
	button:disabled {
		color: var(--ink-500);
		cursor: default;
	}
	.link {
		color: var(--muted-foreground);
	}
	.bad,
	.err {
		color: var(--primary);
	}
	.err {
		font-size: 0.875rem;
		overflow-wrap: anywhere;
	}
	h2 {
		font-size: 1rem;
		font-weight: 400;
		color: var(--muted-foreground);
		margin: 2.5rem 0 0.25rem;
	}
	.jobs {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}
	.jobs li {
		display: flex;
		gap: 0.75rem;
		flex-wrap: wrap;
	}
	.logs {
		margin: 0.5rem 0 0;
		font-size: 0.875rem;
	}
	.del {
		display: flex;
		gap: 1.5rem;
		align-items: baseline;
		margin-top: 3rem;
	}
</style>
