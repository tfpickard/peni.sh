<script lang="ts">
	import { onMount } from 'svelte';
	import * as ops from '../../../lib/admin-ops';

	let s = $state<ops.System | null>(null);
	let note = $state('');
	let now = $state(Date.now());

	onMount(() => {
		ops.system().then(
			(r) => (s = r),
			() => (note = 'not here')
		);
		const t = setInterval(() => (now = Date.now()), 1000);
		return () => clearInterval(t);
	});

	const beat = $derived(
		s?.worker_heartbeat_at ? Math.max(0, Math.round((now - Date.parse(s.worker_heartbeat_at)) / 1000)) : null
	);
	const gb = (n: number) => (n / 1024 ** 3).toFixed(1) + ' GB';
</script>

{#if s}
	<dl>
		<dt>deployed</dt>
		<dd>{s.deployed_sha ? s.deployed_sha.slice(0, 7) : 'none'}</dd>
		<dt>worker</dt>
		<dd class:hot={beat !== null && beat > 120}>{beat === null ? 'none' : `${beat}s ago`}</dd>
		<dt>disk</dt>
		<dd>{gb(s.disk.used)} / {gb(s.disk.total)} {Math.round((s.disk.used / s.disk.total) * 100)}%</dd>
		<dt>db</dt>
		<dd>{gb(s.db_size)}</dd>
		<dt>backup</dt>
		<dd>{s.last_backup_at ? new Date(s.last_backup_at).toISOString().slice(0, 16).replace('T', ' ') : 'none'}</dd>
	</dl>

	<h2>errors</h2>
	{#if s.recent_errors.length}
		<ul class="plain tight">
			{#each s.recent_errors as e}<li>
					<div class="dim">{e.type} {e.id} {e.status} {e.attempts}</div>
					<pre>{e.error}</pre>
				</li>{/each}
		</ul>
	{:else}
		<p class="dim">0 errors</p>
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
	.tight {
		gap: 0.75rem;
	}
	pre {
		margin: 0;
		font-family: ui-monospace, 'SF Mono', Menlo, monospace;
		font-size: 0.875rem;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
</style>
