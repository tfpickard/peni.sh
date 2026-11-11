<script lang="ts">
	import { onMount } from 'svelte';
	import * as ops from '../../../lib/admin-ops';

	let source = $state('');
	let active = $state('');
	let items = $state<ops.Haiku[]>([]);
	let total = $state(0);
	// The api's own list. Learning it from the rows shown missed any source not on page one.
	const sources = ['seed', 'model', 'owner'];
	let loaded = $state(false);
	let editing = $state<number | null>(null);
	let draft = $state(['', '', '']);
	let add = $state(['', '', '']);
	let n = $state(20);
	let queued = $state(false);

	const PAGE = 50;
	// The api's cap on one request.
	const CHUNK = 200;
	// How many rows the list should hold. An edit reloads this many, so it keeps its place.
	let want = PAGE;
	// A slow reload must not overwrite the list a later filter change asked for.
	let epoch = 0;

	async function load() {
		const e = ++epoch;
		try {
			const got: ops.Haiku[] = [];
			let all = 0;
			do {
				const page = await ops.haikus(source, active, Math.min(CHUNK, want - got.length), got.length);
				if (e !== epoch) return;
				all = page.total;
				got.push(...page.items);
				if (!page.items.length) break;
			} while (got.length < Math.min(want, all));
			items = got;
			total = all;
		} catch {
			if (e !== epoch) return;
			items = [];
			total = 0;
		}
		loaded = true;
	}

	function filter() {
		want = PAGE;
		load();
	}

	function more() {
		want = items.length + PAGE;
		load();
	}

	onMount(load);

	function edit(h: ops.Haiku) {
		editing = h.id;
		draft = [...h.lines, '', '', ''].slice(0, 3);
	}

	async function save(h: ops.Haiku) {
		await ops.patchHaiku(h.id, { lines: draft });
		editing = null;
		load();
	}

	async function toggle(h: ops.Haiku) {
		await ops.patchHaiku(h.id, { active: !h.active });
		load();
	}

	async function remove(h: ops.Haiku) {
		await ops.deleteHaiku(h.id);
		load();
	}

	async function create(e: SubmitEvent) {
		e.preventDefault();
		if (add.some((l) => !l.trim())) return;
		await ops.addHaiku(add.map((l) => l.trim()));
		add = ['', '', ''];
		load();
	}

	async function grow() {
		await ops.growHaikus(n);
		queued = true;
	}
</script>

<div class="row filters">
	<select bind:value={source} onchange={filter} aria-label="source">
		<option value="">any source</option>
		{#each sources as s}<option value={s}>{s}</option>{/each}
	</select>
	<select bind:value={active} onchange={filter} aria-label="active">
		<option value="">any</option>
		<option value="true">active</option>
		<option value="false">inactive</option>
	</select>
</div>

{#if items.length}
	<ul class="plain">
		{#each items as h (h.id)}
			<li class="item">
				{#if editing === h.id}
					<div class="edit">
						{#each draft as _, i}<input bind:value={draft[i]} aria-label="line {i + 1}" />{/each}
						<div class="row">
							<button onclick={() => save(h)}>save</button>
							<button class="link" onclick={() => (editing = null)}>cancel</button>
						</div>
					</div>
				{:else}
					<p class:dim={!h.active}>
						{#each h.lines as l}{l}<br />{/each}
					</p>
					<div class="row">
						<span class="dim">{h.source}</span>
						<button class:dim={!h.active} onclick={() => toggle(h)}>
							{h.active ? 'active' : 'inactive'}
						</button>
						<button onclick={() => edit(h)}>edit</button>
						<button class="link" onclick={() => remove(h)}>delete</button>
					</div>
				{/if}
			</li>
		{/each}
	</ul>
	{#if total > items.length}
		<div class="row">
			<span class="dim">{items.length} of {total}</span>
			<button onclick={more}>more</button>
		</div>
	{/if}
{:else if loaded}
	<p class="dim">0 haikus</p>
{/if}

<h2>add</h2>
<form class="edit" onsubmit={create}>
	{#each add as _, i}<input bind:value={add[i]} aria-label="line {i + 1}" placeholder="line {i + 1}" />{/each}
	<div class="row"><button>add</button></div>
</form>

<h2>grow</h2>
<div class="row">
	<input type="number" min="1" bind:value={n} aria-label="count" class="n" />
	<button onclick={grow}>grow</button>
	{#if queued}<span class="dim">queued</span>{/if}
</div>

<style>
	.filters {
		margin-bottom: 2rem;
	}
	.item p {
		margin: 0 0 0.25rem;
	}
	.edit {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		max-width: 32rem;
	}
	.n {
		width: 5rem;
	}
</style>
