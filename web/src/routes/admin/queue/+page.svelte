<script lang="ts">
	import { onMount } from 'svelte';
	import * as ops from '../../../lib/admin-ops';

	let q = $state<ops.Queue>({ jobs: [], failed: [] });
	let open = $state<Set<number>>(new Set());
	let loaded = $state(false);

	async function load() {
		try {
			q = await ops.queue();
		} catch {
			// A missed refresh keeps the last list; the next tick tries again.
		}
		loaded = true;
	}

	onMount(() => {
		load();
		const t = setInterval(() => {
			if (document.visibilityState === 'visible') load();
		}, 5000);
		return () => clearInterval(t);
	});

	function expand(id: number) {
		const s = new Set(open);
		if (!s.delete(id)) s.add(id);
		open = s;
	}

	const act = (fn: () => Promise<unknown>) => async () => {
		await fn();
		load();
	};
	const trunc = (s: string) => (s.length > 60 ? s.slice(0, 60) + '...' : s);
</script>

{#if q.jobs.length}
	<table>
		<thead>
			<tr><th>id</th><th>type</th><th>status</th><th>attempts</th><th>run_after</th><th>last_error</th><th></th></tr>
		</thead>
		<tbody>
			{#each q.jobs as j (j.id)}
				<tr>
					<td>{j.id}</td>
					<td>{j.type}</td>
					<td class:hot={j.status === 'failed'}>{j.status}</td>
					<td>{j.attempts}</td>
					<td class="dim">{j.run_after ?? ''}</td>
					<td class="err">
						{#if j.last_error}
							<button class="link txt" onclick={() => expand(j.id)}>
								{open.has(j.id) ? j.last_error : trunc(j.last_error)}
							</button>
						{/if}
					</td>
					<td class="acts">
						<button onclick={act(() => ops.retryJob(j.id))}>retry</button>
						<button class="link" onclick={act(() => ops.deleteJob(j.id))}>delete</button>
					</td>
				</tr>
			{/each}
		</tbody>
	</table>
{:else if loaded}
	<p class="dim">0 jobs</p>
{/if}

<h2>failed</h2>
{#if q.failed.length}
	<ul class="plain">
		{#each q.failed as f (f.name)}
			<li>
				<div class="row">
					<span>{f.name}</span>
					<button onclick={act(() => ops.retryFailed(f.name))}>retry</button>
					<button class="link" onclick={act(() => ops.deleteFailed(f.name))}>delete</button>
				</div>
				<pre class="dim">{f.err}</pre>
			</li>
		{/each}
	</ul>
{:else if loaded}
	<p class="dim">0 files</p>
{/if}

<style>
	.err {
		max-width: 24rem;
		overflow-wrap: anywhere;
	}
	.txt {
		text-align: left;
		white-space: pre-wrap;
	}
	.acts {
		white-space: nowrap;
		display: flex;
		gap: 1rem;
	}
	pre {
		margin: 0.25rem 0 0;
		font-family: ui-monospace, 'SF Mono', Menlo, monospace;
		font-size: 0.875rem;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
</style>
