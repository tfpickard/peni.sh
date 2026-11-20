<script lang="ts">
	import { onMount } from 'svelte';
	import * as admin from '../../admin';
	import * as cols from '../../admin-collections';
	import * as uq from '../../upload-queue';
	import { roman } from '../../roman';

	type Row = uq.Item & { preview: string };

	let { ondone }: { ondone?: () => void } = $props();

	let rows = $state<Row[]>([]);
	let fields = $state<admin.Fields>({ caption: '', description: '', tags: '', prompt: '' });
	let over = $state(false);
	let halted = $state(false);
	// One sidecar names one collection, so ticking one unticks any other.
	let collections = $state<cols.Summary[]>([]);
	const noFiling: admin.Filing = { collection: '', book: '', leaf: '', note: '' };
	let filing = $state<admin.Filing>({ ...noFiling });
	const chosen = $derived(collections.find((c) => c.slug === filing.collection) ?? null);

	// Rows already posted keep their own text and filing; the form speaks only for the rest.
	const fresh = $derived(rows.filter((r) => r.phase === 'new'));
	const single = $derived(fresh.length === 1);
	// Text is per picture, so it only stands down when several are chosen. With none
	// chosen yet the fields are open: Tom often writes the caption first.
	const many = $derived(fresh.length > 1);
	const sending = $derived(rows.some(uq.isSending));
	const failed = $derived(rows.some((r) => r.phase === 'failed'));
	const line = $derived(uq.summary(rows));

	const queue = new uq.UploadQueue({
		// The filing holds until the batch it was set for is out, unless more pictures are
		// already waiting under it.
		ondrain: () => {
			if (!fresh.length) filing = { ...noFiling };
		},
		ondone: () => ondone?.(),
		onhalt: (h) => (halted = h)
	});

	onMount(() => {
		queue.setVisible(document.visibilityState === 'visible');
		queue.setOnline(navigator.onLine);
		(async () => {
			try {
				collections = (await cols.list()).collections;
			} catch {
				collections = [];
			}
		})();
		return () => {
			letGo();
			for (const r of rows) URL.revokeObjectURL(r.preview);
		};
	});

	// The browser drops a wake lock whenever the page is hidden, so it is asked for again
	// on every return while anything is still going out.
	let lock: WakeLockSentinel | null = null;
	let asking = false;
	async function hold() {
		if (lock || asking || !('wakeLock' in navigator)) return;
		if (document.visibilityState !== 'visible') return;
		asking = true;
		try {
			const l = await navigator.wakeLock.request('screen');
			l.addEventListener('release', () => {
				if (lock === l) lock = null;
			});
			if (sending) lock = l;
			else void l.release();
		} catch {
			// Refused, by a low battery or a browser that will not; the sends go on regardless.
		} finally {
			asking = false;
		}
	}
	function letGo() {
		const l = lock;
		lock = null;
		void l?.release();
	}

	$effect(() => {
		if (sending) hold();
		else letGo();
	});

	function seen() {
		const v = document.visibilityState === 'visible';
		queue.setVisible(v);
		if (v && sending) hold();
	}

	function warn(e: BeforeUnloadEvent) {
		if (!sending) return;
		e.preventDefault();
		// Older Safari and Chrome only ask when this is set.
		e.returnValue = '';
	}

	function tick(slug: string) {
		filing = filing.collection === slug ? { ...noFiling } : { ...noFiling, collection: slug };
	}

	// iPads hand over HEIC, and some browsers give it no type at all.
	const HEIC = /\.(heic|heif)$/i;
	const isImage = (f: File) => f.type.startsWith('image/') || (!f.type && HEIC.test(f.name));
	const same = (a: File, b: File) =>
		a.name === b.name && a.size === b.size && a.lastModified === b.lastModified;

	// A finished batch is replaced by the next pick. While one is still moving, new pictures
	// join it and only the unposted ones are swapped.
	function pick(files: FileList | File[] | null) {
		if (!files) return;
		const keep = queue.clear() ? [] : rows.filter((r) => r.phase !== 'new');
		for (const r of rows) if (!keep.includes(r)) URL.revokeObjectURL(r.preview);
		const added: Row[] = [];
		for (const file of files) {
			if (!isImage(file)) continue;
			if ([...keep, ...added].some((r) => same(r.file, file))) continue;
			added.push({ ...uq.item(file), preview: URL.createObjectURL(file) });
		}
		rows = [...keep, ...added];
	}

	function drop(e: DragEvent) {
		e.preventDefault();
		over = false;
		pick(e.dataTransfer?.files ?? null);
	}

	function post(e: SubmitEvent) {
		e.preventDefault();
		const batch = fresh;
		if (!batch.length) return;
		const text = single ? { ...fields } : null;
		// A leaf is one slot, and a note is about one leaf; several pictures can share a book
		// but neither of those.
		const f = filing.collection
			? { ...filing, leaf: single ? filing.leaf : '', note: single ? filing.note : '' }
			: null;
		for (const r of batch) {
			r.fields = text;
			r.filing = f ? { ...f } : null;
		}
		fields = { caption: '', description: '', tags: '', prompt: '' };
		queue.add(batch);
	}
</script>

<svelte:window
	onbeforeunload={warn}
	ononline={() => queue.setOnline(true)}
	onoffline={() => queue.setOnline(false)}
/>
<svelte:document onvisibilitychange={seen} />

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
		accept="image/png,image/jpeg,image/webp,image/heic,image/heif,.heic,.heif"
		multiple
		aria-label="image"
		onchange={(e) => pick(e.currentTarget.files)}
	/>

	{#if halted}<p class="hot">{uq.SESSION_ENDED}</p>{/if}
	{#if line}<p class="dim">{line}</p>{/if}

	{#if rows.length}
		<ul class="rows">
			{#each rows as row (row.preview)}
				<li>
					<img src={row.preview} alt="" />
					<span class="name">{row.name}</span>
					{#if row.phase === 'uploading'}
						<progress value={row.progress} max="1"></progress>
					{:else if row.phase === 'hashing' || row.phase === 'checking'}
						<progress></progress>
					{:else if row.phase === 'queued'}
						<span class="dim">queued</span>
					{:else if row.phase === 'failed'}
						<span class="bad">failed</span>
						{#if row.error}<span class="err">{row.error}</span>{/if}
					{:else if row.state === 'live' && row.slug}
						<a href="/i/{row.slug}">live</a>
					{:else if row.state}
						<span class:bad={row.state === 'failed'}>{row.state}</span>
						{#if row.error}<span class="err">{row.error}</span>{/if}
						{#if row.quiet}<a href="/admin/queue">queue</a>{/if}
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
		<button class="go" disabled={!fresh.length}>post</button>
		{#if failed}
			<button type="button" class="link" onclick={() => queue.retryFailed()}>retry failed</button>
		{/if}
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
