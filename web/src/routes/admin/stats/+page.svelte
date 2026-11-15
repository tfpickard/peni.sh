<script lang="ts">
	import { onMount } from 'svelte';
	import * as ops from '../../../lib/admin-ops';
	import { spark } from '../../../lib/spark';

	let s = $state<ops.Stats | null>(null);
	let note = $state('');

	onMount(async () => {
		try {
			s = await ops.stats();
		} catch {
			note = 'not here';
		}
	});

	const usage = $derived(s?.api_usage.last_30d ?? []);
	const totalIn = $derived(usage.reduce((a, u) => a + u.in, 0));
	const totalOut = $derived(usage.reduce((a, u) => a + u.out, 0));
	const fmt = (n: number) => n.toLocaleString('en-US');
</script>

{#if s}
	<dl>
		<dt>pictures</dt>
		<dd>{fmt(s.pictures)}</dd>
		{#each Object.entries(s.by_status) as [k, v]}
			<dt>{k}</dt>
			<dd>{fmt(v)}</dd>
		{/each}
		<dt>votes</dt>
		<dd>{fmt(s.votes_total)}</dd>
		<dt>comments pending</dt>
		<dd>{fmt(s.comments_pending)}</dd>
		{#if s.temperature_history.length > 1}
			<dt>temperature</dt>
			<dd>
				<svg viewBox="0 0 200 24" width="200" height="24" aria-label="temperature history">
					<polyline points={spark(s.temperature_history.map((h) => h.temperature))} fill="none" stroke="var(--secondary)" stroke-width="1" />
				</svg>
			</dd>
		{/if}
	</dl>

	<div class="cols">
		{#each [{ label: 'top', list: s.top }, { label: 'bottom', list: s.bottom }] as { label, list } (label)}
			<div>
				<h2>{label}</h2>
				<ul class="plain tight">
					{#each list as r}
						<li class="row">
							{#if r.thumb}<img class="thumb" src={r.thumb} alt="" />{:else}<span class="thumb"></span>{/if}
							<a href="/i/{r.slug}">{r.slug}</a>
							<span class="dim">{r.net}</span>
						</li>
					{/each}
				</ul>
			</div>
		{/each}
	</div>

	<h2>api usage</h2>
	{#if usage.length}
		<table>
			<thead><tr><th>day</th><th>model</th><th class="num">in</th><th class="num">out</th></tr></thead>
			<tbody>
				{#each usage as u}
					<tr><td>{u.day}</td><td>{u.model}</td><td class="num">{fmt(u.in)}</td><td class="num">{fmt(u.out)}</td></tr>
				{/each}
			</tbody>
			<tfoot>
				<tr class="dim"><td>total</td><td></td><td class="num">{fmt(totalIn)}</td><td class="num">{fmt(totalOut)}</td></tr>
			</tfoot>
		</table>
	{:else}
		<p class="dim">0 calls</p>
	{/if}
{:else if note}
	<p class="dim">{note}</p>
{/if}

<style>
	dl {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 0.25rem 2rem;
		margin: 0;
	}
	dt {
		color: var(--muted-foreground);
	}
	dd {
		margin: 0;
		font-variant-numeric: tabular-nums;
	}
	.cols {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 2rem;
	}
	.tight {
		gap: 0.5rem;
	}
	table {
		max-width: 32rem;
	}
</style>
