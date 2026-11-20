<script lang="ts">
	import { onMount } from 'svelte';
	import Upload from '../../../lib/components/admin/Upload.svelte';
	import * as pics from '../../../lib/admin-pictures';

	let q = $state('');
	let status = $state('');
	let nudity = $state('');
	let armed = $state(false);
	let sort = $state('new');
	let items = $state<pics.Item[]>([]);
	let cursor = $state<string | null>(null);
	let loading = $state(false);
	let done = $state(false);
	let note = $state('');
	let sentinel = $state<HTMLElement>();
	let picked = $state(new Set<number>());
	let anchor: number | null = null;
	let busy = $state(false);
	let dialog = $state<HTMLDialogElement>();

	// Thumbnail size, remembered per browser. Storage can be missing or refuse (private
	// mode), and then the page just starts at m.
	const SIZES = { s: '3rem', m: '6rem', l: '10rem' } as const;
	type Size = keyof typeof SIZES;
	const SIZE_KEY = 'admin.pictures.thumbs';
	let size = $state<Size>('m');
	function setSize(s: Size) {
		size = s;
		try {
			localStorage.setItem(SIZE_KEY, s);
		} catch {
			// Not remembered; the choice still holds for this visit.
		}
	}

	// A newer query invalidates responses still in flight for an older one.
	let gen = 0;
	let timer: ReturnType<typeof setTimeout> | undefined;

	async function more(reset: boolean) {
		if (reset) {
			gen++;
			items = [];
			picked = new Set();
			anchor = null;
			cursor = null;
			done = false;
			note = '';
		} else if (loading || done) return;
		const mine = gen;
		loading = true;
		try {
			const page = await pics.list({ q: q.trim(), status, nudity, sort, cursor });
			if (mine !== gen) return;
			items = reset ? page.items : [...items, ...page.items];
			cursor = page.next_cursor;
			done = !page.next_cursor;
		} catch (e) {
			if (mine !== gen) return;
			done = true;
			note = e instanceof pics.HttpError && e.status === 503 ? 'search unavailable' : 'failed';
		} finally {
			if (mine === gen) loading = false;
		}
	}

	const reload = () => more(true);

	function typed() {
		clearTimeout(timer);
		timer = setTimeout(reload, 300);
	}

	onMount(() => {
		try {
			const s = localStorage.getItem(SIZE_KEY);
			if (s && s in SIZES) size = s as Size;
		} catch {
			// Default size.
		}
		reload();
		const io = new IntersectionObserver((e) => e[0].isIntersecting && more(false), {
			rootMargin: '600px'
		});
		if (sentinel) io.observe(sentinel);
		return () => {
			io.disconnect();
			clearTimeout(timer);
		};
	});

	// Shift-click picks the run of rows between the last click and this one.
	function pick(id: number, e: MouseEvent) {
		const next = new Set(picked);
		const on = !picked.has(id);
		const from = anchor === null ? -1 : items.findIndex((i) => i.id === anchor);
		const to = items.findIndex((i) => i.id === id);
		if (e.shiftKey && from >= 0 && to >= 0) {
			for (const it of items.slice(Math.min(from, to), Math.max(from, to) + 1)) {
				if (on) next.add(it.id);
				else next.delete(it.id);
			}
		} else if (on) next.add(id);
		else next.delete(id);
		anchor = id;
		picked = next;
	}

	const all = $derived(items.length > 0 && items.every((i) => picked.has(i.id)));
	function pickAll() {
		picked = all ? new Set() : new Set(items.map((i) => i.id));
		anchor = null;
	}

	const LABEL: Record<Exclude<pics.BulkAction, 'delete'>, string> = {
		redescribe: 'redescribe',
		retag: 'retag',
		rehaiku: 'rehaiku',
		reembed: 're-embed',
		nudity: 'detect nudity'
	};

	// Every picture costs a model call, so the first press only arms it.
	async function detectAll() {
		if (!armed) {
			armed = true;
			return;
		}
		armed = false;
		try {
			await pics.nudityAll();
			note = 'queued';
		} catch {
			note = 'failed';
		}
	}

	async function run(action: pics.BulkAction) {
		if (busy || !picked.size) return;
		busy = true;
		const ids = [...picked];
		let done = 0;
		let queued = 0;
		let failed = false;
		try {
			for (let i = 0; i < ids.length; i += pics.BULK_MAX) {
				const r = await pics.bulk(action, ids.slice(i, i + pics.BULK_MAX));
				done += r.done.length;
				queued += r.queued?.length ?? 0;
			}
			picked = new Set();
			anchor = null;
		} catch {
			failed = true;
		}
		try {
			// The list pages by offset, and deleted rows shift every later one; start over.
			if (action === 'delete' && done) await more(true);
		} finally {
			busy = false;
		}
		if (action === 'delete') note = failed ? `${done} deleted, then failed` : `${done} deleted`;
		else note = failed ? 'failed' : `${queued} queued`;
	}

	function confirmDelete() {
		dialog?.close();
		run('delete');
	}

	const day = (s: string) => s.slice(0, 10);
	const net = (n: number) => (n > 0 ? `+${n}` : String(n));
</script>

<svelte:head>
	<title>pictures -- peni.sh</title>
	<meta name="robots" content="noindex" />
</svelte:head>

<Upload ondone={reload} />

<div class="filters">
	<input type="search" placeholder="search" aria-label="search" bind:value={q} oninput={typed} />
	<select aria-label="status" bind:value={status} onchange={reload}>
		<option value="">all</option>
		<option value="live">live</option>
		<option value="pending_enrichment">pending_enrichment</option>
		<option value="processing">processing</option>
		<option value="failed">failed</option>
		<option value="unpublished">unpublished</option>
	</select>
	<select aria-label="nudity" bind:value={nudity} onchange={reload}>
		<option value="">nudity</option>
		<option value="nude">nude</option>
		<option value="clean">clean</option>
		<option value="unchecked">unchecked</option>
	</select>
	<select aria-label="sort" bind:value={sort} onchange={reload}>
		<option value="new">new</option>
		<option value="old">old</option>
		<option value="votes">votes</option>
		<option value="lonely">lonely</option>
	</select>
	<div class="sizes" role="group" aria-label="thumbnail size">
		{#each Object.keys(SIZES) as Size[] as k (k)}
			<button class:on={size === k} class:link={size !== k} aria-pressed={size === k} onclick={() => setSize(k)}
				>{k}</button
			>
		{/each}
	</div>
</div>

{#if note}<p class="dim">{note}</p>{/if}

{#if items.length}
	<div class="bar" class:on={picked.size > 0}>
		<label class="hit"><input type="checkbox" checked={all} disabled={busy} onchange={pickAll} aria-label="all" /></label>
		{#if picked.size}
			<span class="dim">{picked.size} selected</span>
			{#each Object.entries(LABEL) as [action, label] (action)}
				<button disabled={busy} onclick={() => run(action as pics.BulkAction)}>{label}</button>
			{/each}
			<button disabled={busy} onclick={() => dialog?.showModal()}>delete</button>
			<button class="dim" disabled={busy} onclick={() => ((picked = new Set()), (anchor = null))}
				>clear</button
			>
		{:else}
			<!-- At the far end, so it never reads as the select-all box's label. -->
			<span class="end-acts">
				{#if armed}<button class="dim" onclick={() => (armed = false)}>cancel</button>{/if}
				<button onclick={detectAll}>{armed ? 'sure' : 'detect nudity in all'}</button>
			</span>
		{/if}
	</div>
{/if}

<dialog bind:this={dialog}>
	<p>delete {picked.size} {picked.size === 1 ? 'picture' : 'pictures'}?</p>
	<div class="acts">
		<button onclick={confirmDelete}>delete</button>
		<button class="dim" onclick={() => dialog?.close()}>cancel</button>
	</div>
</dialog>

<table style:--t={SIZES[size]}>
	<tbody>
		{#each items as it (it.id)}
			<tr class:sel={picked.has(it.id)}>
				<td class="box">
					<!-- The label is the 44px target; a tap on it clicks the box. -->
					<label class="hit">
						<input
							type="checkbox"
							checked={picked.has(it.id)}
							disabled={busy}
							onclick={(e) => pick(it.id, e)}
							aria-label="select {it.caption ?? it.slug ?? it.id}"
						/>
					</label>
				</td>
				<td class="th">
					<a href="/admin/pictures/{it.id}" aria-label={it.caption ?? it.slug ?? String(it.id)}>
						{#if it.thumb}<img src={it.thumb} alt="" loading="lazy" />{:else}<span class="blank"></span>{/if}
					</a>
				</td>
				<td class="cap">
					<a href="/admin/pictures/{it.id}">{it.caption ?? it.slug ?? it.id}</a>
					<!-- The columns on the right, folded under the caption where there is no room. -->
					<span class="sub" class:bad={it.status === 'failed'}>
						{it.status} / {net(it.net)} / <span class="nowrap">{day(it.created_at)}</span>
					</span>
				</td>
				<td class="nude">{it.nudity?.length ? 'nude' : ''}</td>
				<td class="meta" class:bad={it.status === 'failed'}>{it.status}</td>
				<td class="meta num">{net(it.net)}</td>
				<td class="meta num">{day(it.created_at)}</td>
			</tr>
		{/each}
	</tbody>
</table>

{#if !items.length && !loading && !note}<p class="dim">0 pictures</p>{/if}
<div bind:this={sentinel} class="end"></div>

<style>
	.filters {
		display: flex;
		gap: 0.75rem 1rem;
		align-items: center;
		flex-wrap: wrap;
		margin: 2.5rem 0 1rem;
	}
	input[type='search'] {
		flex: 1;
		min-width: 10rem;
		appearance: none;
	}
	.sizes {
		display: flex;
		gap: 0;
	}
	.sizes button {
		min-width: 44px;
		min-height: 44px;
	}
	table {
		width: 100%;
		border-collapse: collapse;
	}
	tr {
		border-bottom: 1px solid var(--ink-800);
	}
	/* tbody, to outrank the layout's shared top-aligned table cells. */
	tbody td {
		padding: 0.5rem 0.75rem 0.5rem 0;
		vertical-align: middle;
	}
	tbody td:last-child {
		padding-right: 0;
	}
	.th {
		width: var(--t);
		line-height: 0;
	}
	.th img,
	.blank {
		display: block;
		width: var(--t);
		height: var(--t);
		object-fit: cover;
		background: var(--muted);
	}
	.cap {
		width: 100%;
		max-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	/* Magenta is for hover and actions; a list of captions in it is a wall of pink. */
	.cap a {
		color: var(--foreground);
	}
	.cap a:hover {
		color: var(--primary);
	}
	.num {
		text-align: right;
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.dim,
	.meta {
		color: var(--muted-foreground);
	}
	.bad {
		color: var(--primary);
	}
	td.meta,
	td.nude {
		font-size: 0.875rem;
		white-space: nowrap;
	}
	td.nude {
		color: var(--primary);
	}
	.end {
		height: 1px;
	}
	.bar {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem 1.25rem;
		padding: 0.25rem 0;
		font-size: 0.875rem;
	}
	.end-acts {
		display: flex;
		gap: 1.25rem;
		margin-left: auto;
	}
	/* Stays in reach while picking far down a long list. */
	.bar.on {
		position: sticky;
		top: 0;
		z-index: 1;
		background: var(--background);
		border-bottom: 1px solid var(--ink-700);
	}
	tbody td.box {
		width: 44px;
		padding: 0 0.25rem 0 0;
		line-height: 0;
	}
	.sub {
		display: none;
		font-size: 0.875rem;
		color: var(--muted-foreground);
	}
	.nowrap {
		white-space: nowrap;
	}
	@media (max-width: 40rem) {
		.cap {
			white-space: normal;
		}
		.cap a {
			display: block;
			overflow-wrap: anywhere;
		}
		.sub {
			display: block;
			margin-top: 0.25rem;
		}
		td.meta {
			display: none;
		}
	}
	tr.sel td {
		background: var(--ink-900);
	}
	button.dim {
		color: var(--muted-foreground);
	}
	dialog {
		background: var(--background);
		color: var(--foreground);
		border: 1px solid var(--ink-700);
		padding: 1.5rem 2rem;
	}
	dialog::backdrop {
		background: rgb(0 0 0 / 0.6);
	}
	dialog p {
		margin: 0 0 1.25rem;
	}
	.acts {
		display: flex;
		gap: 1.5rem;
	}
</style>
