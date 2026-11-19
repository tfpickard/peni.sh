<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import Picture from '../../../../lib/components/Picture.svelte';
	import Cropper from '../../../../lib/components/admin/Cropper.svelte';
	import * as pics from '../../../../lib/admin-pictures';
	import * as col from '../../../../lib/admin-collections';
	import { roman } from '../../../../lib/collections';

	let { data } = $props();
	const id = $derived(data.id);

	let d = $state<pics.Detail | null>(null);
	let missing = $state(false);
	let msg = $state('');
	let cropping = $state(false);
	let confirming = $state(false);
	let typed = $state('');

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
			d = x;
		} catch (e) {
			if (!d) missing = true;
			else if (!(e instanceof pics.HttpError)) msg = 'failed';
		}
	}

	// One row per collection: in it or not, and the book and leaf to pin it to.
	let cols = $state<col.PictureCollection[]>([]);
	let colDraft = $state<Record<string, { on: boolean; book: string; leaf: string }>>({});
	async function loadCols() {
		try {
			cols = (await col.forPicture(id)).collections;
			colDraft = Object.fromEntries(
				cols.map((x) => [x.slug, { on: Boolean(x.leaf || x.request), book: '', leaf: '' }])
			);
		} catch {
			cols = [];
		}
	}
	const colState = (x: col.PictureCollection) =>
		x.leaf
			? `${roman(x.leaf.book)}, leaf ${x.leaf.position}${x.leaf.pinned_position ? ', pinned' : x.leaf.pinned_book ? ', pinned to book' : ''}`
			: x.request
				? (x.request.error ?? 'waiting')
				: '';
	async function saveCol(x: col.PictureCollection) {
		const dft = colDraft[x.slug];
		msg = '';
		try {
			if (!dft.on) await col.unfile(x.slug, id);
			else await col.place(x.slug, id, dft.book, dft.leaf);
		} catch (e) {
			msg = e instanceof col.CollectionError ? e.message : 'failed';
		}
		await Promise.all([loadCols(), load()]);
	}

	onMount(() => {
		load();
		loadCols();
	});

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

			{#each cols as x (x.slug)}
				{@const dft = colDraft[x.slug]}
				{#if dft}
					<div class="row coll">
						<label class="kind"><input type="checkbox" bind:checked={dft.on} /> {x.slug}</label>
						<input class="num" placeholder="book" aria-label="book" bind:value={dft.book} disabled={!dft.on} />
						<input class="num" placeholder="leaf" aria-label="leaf" bind:value={dft.leaf} disabled={!dft.on} />
						<button
							disabled={dft.on === Boolean(x.leaf || x.request) && !dft.book.trim() && !dft.leaf.trim()}
							onclick={() => saveCol(x)}>save</button
						>
						<span class="dim" class:bad={Boolean(x.request?.error)}>{colState(x)}</span>
					</div>
				{/if}
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
	.num {
		width: 4rem;
	}
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
	input,
	textarea {
		background: transparent;
		color: var(--foreground);
		border: 0;
		border-bottom: 1px solid var(--ink-700);
		border-radius: 0;
		font: inherit;
		padding: 0.4rem 0;
		resize: vertical;
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
	.row {
		display: flex;
		gap: 1.5rem;
		align-items: baseline;
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
