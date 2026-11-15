<script lang="ts">
	import { onMount } from 'svelte';
	import Upload from '../../../lib/components/admin/Upload.svelte';
	import * as pics from '../../../lib/admin-pictures';

	let q = $state('');
	let status = $state('');
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
			const page = await pics.list({ q: q.trim(), status, sort, cursor });
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
		reembed: 're-embed'
	};

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
	<select aria-label="sort" bind:value={sort} onchange={reload}>
		<option value="new">new</option>
		<option value="old">old</option>
		<option value="votes">votes</option>
		<option value="lonely">lonely</option>
	</select>
</div>

{#if note}<p class="dim">{note}</p>{/if}

{#if items.length}
	<div class="bar" class:on={picked.size > 0}>
		<label class="box"><input type="checkbox" checked={all} disabled={busy} onchange={pickAll} aria-label="all" /></label>
		{#if picked.size}
			<span class="dim">{picked.size} selected</span>
			{#each Object.entries(LABEL) as [action, label] (action)}
				<button disabled={busy} onclick={() => run(action as pics.BulkAction)}>{label}</button>
			{/each}
			<button disabled={busy} onclick={() => dialog?.showModal()}>delete</button>
			<button class="dim" disabled={busy} onclick={() => ((picked = new Set()), (anchor = null))}
				>clear</button
			>
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

<table>
	<tbody>
		{#each items as it (it.id)}
			<tr class:sel={picked.has(it.id)}>
				<td class="box">
					<input
						type="checkbox"
						checked={picked.has(it.id)}
						disabled={busy}
						onclick={(e) => pick(it.id, e)}
						aria-label="select {it.caption ?? it.slug ?? it.id}"
					/>
				</td>
				<td class="th">
					<a href="/admin/pictures/{it.id}" aria-label={it.caption ?? it.slug ?? String(it.id)}>
						{#if it.thumb}<img src={it.thumb} alt="" loading="lazy" />{:else}<span class="blank"></span>{/if}
					</a>
				</td>
				<td class="cap"><a href="/admin/pictures/{it.id}">{it.caption ?? it.slug ?? it.id}</a></td>
				<td class="dim" class:bad={it.status === 'failed'}>{it.status}</td>
				<td class="dim num">{net(it.net)}</td>
				<td class="dim num">{day(it.created_at)}</td>
			</tr>
		{/each}
	</tbody>
</table>

{#if !items.length && !loading && !note}<p class="dim">0 pictures</p>{/if}
<div bind:this={sentinel} class="end"></div>

<style>
	.filters {
		display: flex;
		gap: 1.5rem;
		align-items: baseline;
		flex-wrap: wrap;
		margin: 2.5rem 0 1rem;
	}
	input,
	select {
		background: transparent;
		color: var(--foreground);
		border: 0;
		border-bottom: 1px solid var(--ink-700);
		font: inherit;
		padding: 0.4rem 0;
		border-radius: 0;
	}
	select {
		color: var(--muted-foreground);
	}
	option {
		background: var(--background);
	}
	input::placeholder {
		color: var(--ink-500);
	}
	input[type='search'] {
		flex: 1;
		min-width: 10rem;
		appearance: none;
	}
	table {
		width: 100%;
		border-collapse: collapse;
	}
	td {
		padding: 0.3rem 0.75rem 0.3rem 0;
		vertical-align: middle;
	}
	td:last-child {
		padding-right: 0;
	}
	.th {
		width: 3rem;
		line-height: 0;
	}
	.th img,
	.blank {
		display: block;
		width: 3rem;
		height: 3rem;
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
	.num {
		text-align: right;
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.dim {
		color: var(--muted-foreground);
	}
	.bad {
		color: var(--primary);
	}
	td.dim {
		font-size: 0.875rem;
		white-space: nowrap;
	}
	.end {
		height: 1px;
	}
	.bar {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem 1.25rem;
		padding: 0.6rem 0;
		font-size: 0.875rem;
	}
	/* Stays in reach while picking far down a long list. */
	.bar.on {
		position: sticky;
		top: 0;
		z-index: 1;
		background: var(--background);
		border-bottom: 1px solid var(--ink-700);
	}
	.box {
		width: 1.5rem;
		line-height: 0;
	}
	input[type='checkbox'] {
		width: 1.1rem;
		height: 1.1rem;
		margin: 0;
		accent-color: var(--primary);
		cursor: pointer;
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
