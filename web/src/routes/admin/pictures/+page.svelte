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

	// A newer query invalidates responses still in flight for an older one.
	let gen = 0;
	let timer: ReturnType<typeof setTimeout> | undefined;

	async function more(reset: boolean) {
		if (reset) {
			gen++;
			items = [];
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

<table>
	<tbody>
		{#each items as it (it.id)}
			<tr>
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
</style>
