<script lang="ts">
	import { onMount } from 'svelte';
	import * as admin from '../../admin';
	import * as cols from '../../admin-collections';
	import { roman } from '../../roman';

	type Row = {
		name: string;
		file: File;
		preview: string;
		progress: number;
		state: string;
		slug?: string;
		error?: string;
	};

	let { ondone }: { ondone?: () => void } = $props();

	let rows = $state<Row[]>([]);
	let fields = $state<admin.Fields>({ caption: '', description: '', tags: '', prompt: '' });
	let busy = $state(false);
	let over = $state(false);
	// One sidecar names one collection, so ticking one unticks any other.
	let collections = $state<cols.Summary[]>([]);
	const noFiling: admin.Filing = { collection: '', book: '', leaf: '', note: '' };
	let filing = $state<admin.Filing>({ ...noFiling });
	const chosen = $derived(collections.find((c) => c.slug === filing.collection) ?? null);

	const single = $derived(rows.length === 1);
	// Text is per picture, so it only stands down when several are chosen. With none
	// chosen yet the fields are open: Tom often writes the caption first.
	const many = $derived(rows.length > 1);

	onMount(async () => {
		try {
			collections = (await cols.list()).collections;
		} catch {
			collections = [];
		}
	});

	function tick(slug: string) {
		filing = filing.collection === slug ? { ...noFiling } : { ...noFiling, collection: slug };
	}

	function pick(files: FileList | File[] | null) {
		if (!files) return;
		for (const r of rows) URL.revokeObjectURL(r.preview);
		rows = [...files]
			.filter((f) => f.type.startsWith('image/'))
			.map((file) => ({
				name: file.name,
				file,
				preview: URL.createObjectURL(file),
				progress: 0,
				state: ''
			}));
	}

	function drop(e: DragEvent) {
		e.preventDefault();
		over = false;
		pick(e.dataTransfer?.files ?? null);
	}

	const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
	const settled = (s: string) => s === 'live' || s === 'failed';

	async function track(row: Row, sha256: string, stem: string) {
		while (true) {
			await sleep(2000);
			try {
				const s = await admin.status(sha256, stem);
				row.state = s.state;
				row.slug = s.slug;
				row.error = s.error;
				if (settled(s.state)) return;
			} catch {
				// A dropped poll is retried; the next one tells the truth.
			}
		}
	}

	async function post(e: SubmitEvent) {
		e.preventDefault();
		if (busy || !rows.length) return;
		busy = true;
		const text = single ? { ...fields } : null;
		const pending: Promise<void>[] = [];
		for (const row of rows) {
			try {
				row.state = 'uploading';
				// A leaf is one slot, and a note is about one leaf; several pictures can share
				// a book but neither of those.
				const f = filing.collection
					? { ...filing, leaf: single ? filing.leaf : '', note: single ? filing.note : '' }
					: null;
				const { sha256, stem } = await admin.upload(row.file, text, (p) => (row.progress = p), f);
				row.progress = 1;
				row.state = 'waiting';
				pending.push(track(row, sha256, stem));
			} catch (err) {
				row.state = 'failed';
				row.error = (err as Error).message;
			}
		}
		busy = false;
		fields = { caption: '', description: '', tags: '', prompt: '' };
		filing = { ...noFiling };
		await Promise.all(pending);
		ondone?.();
	}
</script>

<form
	class="up"
	class:over
	onsubmit={post}
	ondragover={(e) => {
		e.preventDefault();
		over = true;
	}}
	ondragleave={() => (over = false)}
	ondrop={drop}
>
	<p class="hint">drop pictures here</p>
	<input
		type="file"
		accept="image/png,image/jpeg,image/webp"
		multiple
		aria-label="image"
		onchange={(e) => pick(e.currentTarget.files)}
	/>

	{#if rows.length}
		<ul class="rows">
			{#each rows as row (row.preview)}
				<li>
					<img src={row.preview} alt="" />
					<span class="name">{row.name}</span>
					{#if row.state === 'uploading'}
						<progress value={row.progress} max="1"></progress>
					{:else if row.state === 'live' && row.slug}
						<a href="/i/{row.slug}">live</a>
					{:else if row.state}
						<span class:bad={row.state === 'failed'}>{row.state}</span>
						{#if row.error}<span class="err">{row.error}</span>{/if}
					{/if}
				</li>
			{/each}
		</ul>
	{/if}

	{#if many}<p class="dim">text applies to one picture</p>{/if}

	<label class="field">
		<span class="l">caption</span>
		<input bind:value={fields.caption} disabled={many} />
	</label>
	<label class="field">
		<span class="l">description</span>
		<textarea rows="5" bind:value={fields.description} disabled={many}></textarea>
	</label>
	<label class="field">
		<span class="l">tags</span>
		<textarea rows="4" placeholder="one per line" bind:value={fields.tags} disabled={many}></textarea>
	</label>
	<label class="field">
		<span class="l">prompt</span>
		<textarea rows="6" bind:value={fields.prompt} disabled={many}></textarea>
	</label>

	{#each collections as c (c.slug)}
		<label class="tick">
			<input type="checkbox" checked={filing.collection === c.slug} onchange={() => tick(c.slug)} />
			{c.slug}
		</label>
	{/each}
	{#if chosen}
		<div class="place">
			<label class="field">
				<span class="l">book</span>
				<select bind:value={filing.book}>
					<option value="">any</option>
					{#each Array.from({ length: chosen.book_count }, (_, i) => i + 1) as n (n)}
						<option value={String(n)}>{roman(n)}</option>
					{/each}
				</select>
			</label>
			<label class="field">
				<span class="l">leaf</span>
				<input
					class="leaf"
					type="number"
					min="1"
					max={chosen.leaves_per_book}
					placeholder="any"
					bind:value={filing.leaf}
					disabled={many || !filing.book}
				/>
			</label>
		</div>
	{/if}

	{#if chosen && single}
		<label class="field">
			<span class="l">conservator note</span>
			<textarea rows="4" bind:value={filing.note}></textarea>
		</label>
	{/if}

	<div class="actions">
		<button class="go" disabled={busy || !rows.length}>post</button>
	</div>
</form>

<style>
	form {
		display: flex;
		flex-direction: column;
		gap: 1rem;
		max-width: 48rem;
	}
	/* Visible at rest, so it reads as a place to drop before anything is dragged. */
	.up {
		padding: 1rem;
		border: 1px dashed var(--ink-500);
	}
	.up.over {
		border-color: var(--secondary);
	}
	.hint {
		margin: 0;
		color: var(--muted-foreground);
	}
	input[type='file'] {
		color: var(--muted-foreground);
		font: inherit;
		min-height: 44px;
	}
	.dim {
		margin: 0;
		color: var(--muted-foreground);
	}
	.tick {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		min-height: 44px;
		cursor: pointer;
	}
	.place {
		display: flex;
		flex-wrap: wrap;
		gap: 1rem 1.5rem;
	}
	.leaf {
		width: 7rem;
	}
	.rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}
	li {
		display: flex;
		align-items: center;
		gap: 0.75rem;
	}
	li img {
		width: 6rem;
		height: 6rem;
		object-fit: cover;
		flex: none;
		background: var(--muted);
	}
	.name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		min-width: 0;
		color: var(--muted-foreground);
	}
	progress {
		width: 6rem;
		accent-color: var(--secondary);
	}
	.bad,
	.err {
		color: var(--primary);
	}
	.err {
		font-size: 0.875rem;
		overflow-wrap: anywhere;
	}
</style>
