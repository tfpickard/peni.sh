<script lang="ts">
	import { onMount } from 'svelte';
	import {
		ApiError,
		getMycelium,
		myceliumAction,
		setTunables,
		type ColonyRow,
		type Mycelium,
		type MyceliumAction,
		type TickPoint
	} from '../../../lib/api';
	import { sparkPoints } from '../../../lib/spark';

	let m = $state<Mycelium | null>(null);
	let note = $state('');
	let busy = $state(false);
	// Edits survive the periodic refresh; only a save or a fresh load replaces them.
	// Untouched names follow the server, so a value saved elsewhere is not posted back.
	let draft = $state<Record<string, number | null>>({});
	let edited = $state<Record<string, true>>({});
	let versionNote = $state('');
	let armed = $state(false);
	let disarm: ReturnType<typeof setTimeout> | undefined;
	// Requests in the order they were sent. A refresh in flight when an action answers
	// was read before the action and would put the old state back.
	let sent = 0;
	let shown = 0;

	function take(next: Mycelium, fresh: boolean, n: number) {
		if (n < shown) return;
		shown = n;
		m = next;
		if (fresh) edited = {};
		const d: Record<string, number | null> = {};
		for (const t of next.tunables) d[t.name] = edited[t.name] ? draft[t.name] : t.value;
		draft = d;
	}

	async function load(fresh = false) {
		const n = ++sent;
		try {
			take(await getMycelium(), fresh, n);
		} catch {
			// A missed refresh keeps the last state on screen.
			if (!m) note = 'not here';
		}
	}

	onMount(() => {
		load(true);
		const t = setInterval(() => {
			if (document.visibilityState === 'visible' && !busy) load();
		}, 30_000);
		return () => {
			clearInterval(t);
			clearTimeout(disarm);
		};
	});

	const why = (e: unknown) => (e instanceof ApiError && e.detail ? e.detail : 'failed');

	async function run(fn: () => Promise<Mycelium>, fresh = false) {
		busy = true;
		note = '';
		const n = ++sent;
		try {
			take(await fn(), fresh, n);
		} catch (e) {
			note = why(e);
		} finally {
			busy = false;
		}
	}

	const act = (a: MyceliumAction) => run(() => myceliumAction(a));

	// A second click within five seconds confirms; no browser dialog.
	function reset() {
		clearTimeout(disarm);
		if (!armed) {
			armed = true;
			disarm = setTimeout(() => (armed = false), 5000);
			return;
		}
		armed = false;
		act('reset');
	}

	const changed = $derived(
		m ? m.tunables.filter((t) => draft[t.name] !== null && draft[t.name] !== undefined && draft[t.name] !== t.value) : []
	);
	const bad = (lo: number, hi: number, int: boolean, v: number | null | undefined) =>
		v === null || v === undefined || Number.isNaN(v) || v < lo || v > hi || (int && !Number.isInteger(v));
	const invalid = $derived(m ? m.tunables.some((t) => bad(t.lo, t.hi, t.int, draft[t.name])) : false);

	async function save() {
		const values: Record<string, number> = {};
		for (const t of changed) values[t.name] = draft[t.name] as number;
		const n = versionNote.trim() || null;
		await run(() => setTunables(values, n), true);
		if (!note) {
			versionNote = '';
			note = 'saved';
		}
	}

	const pad = (n: number) => String(n).padStart(2, '0');
	function when(iso: string | null): string {
		if (!iso) return '';
		const d = new Date(iso);
		return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
	}
	const num = (v: number) => (Number.isInteger(v) ? v.toLocaleString('en-US') : v.toFixed(3));
	const values = (v: Record<string, number>) =>
		Object.entries(v)
			.map(([k, x]) => `${k} ${x}`)
			.join(', ') || 'defaults';

	const METRICS = [
		'live',
		'in_gallery',
		'visible',
		'latched',
		'mean_strength',
		'crossings',
		'traffic'
	] as const satisfies readonly (keyof TickPoint)[];

	// Genesis ticks come first and are drawn dim, so replayed history is never read as lived.
	function lines(series: TickPoint[], key: (typeof METRICS)[number]) {
		const pts = sparkPoints(series.map((p) => p[key]));
		const cut = series.findLastIndex((p) => p.simulated);
		if (cut < 0) return { sim: '', live: pts.join(' ') };
		return { sim: pts.slice(0, cut + 1).join(' '), live: pts.slice(cut).join(' ') };
	}

	const genesisOpen = $derived(
		m ? !m.running && !m.public && m.genesis.status !== 'queued' && m.genesis.status !== 'running' : false
	);
	const name = (c: ColonyRow) => c.title ?? (c.serial !== null ? `gallery ${c.serial}` : `colony ${c.id}`);
</script>

{#if m}
	<div class="row">
		<span class:hot={m.running}>{m.running ? 'running' : 'stopped'}</span>
		<span class="dim">tick {m.tick ?? 'none'}</span>
		{#if m.next_due}<span class="dim">next {when(m.next_due)}</span>{/if}
		{#if m.behind > 0}<span>behind {m.behind}</span>{/if}
		<span class="dim">graph {m.graph_version}{m.graph_dirty ? ' dirty' : ''}</span>
		{#if m.simulated_through !== null}<span class="dim">simulated through {m.simulated_through}</span>{/if}
		{#if m.public}<span class="dim">public</span>{/if}
	</div>

	<div class="row acts">
		<button disabled={busy || m.running} onclick={() => act('start')}>start</button>
		<button disabled={busy || !m.running} onclick={() => act('pause')}>pause</button>
		<button class="link" disabled={busy} onclick={() => act('regrow')}>regrow</button>
		<button class="link" class:hot={armed} disabled={busy} onclick={reset}>{armed ? 'sure' : 'reset'}</button>
		<button class="link" disabled={busy || !genesisOpen} onclick={() => act('genesis')}>genesis</button>
		{#if m.genesis.status !== 'none'}
			<span class="dim">genesis {m.genesis.status}{m.genesis.error ? `: ${m.genesis.error}` : ''}</span>
		{/if}
	</div>
	{#if note}<p class="dim">{note}</p>{/if}

	<h2>ticks</h2>
	{#if m.series.length > 1}
		{@const last = m.series[m.series.length - 1]}
		<dl>
			{#each METRICS as k}
				{@const l = lines(m.series, k)}
				<dt>{k.replace('_', ' ')}</dt>
				<dd>
					<svg viewBox="0 0 200 24" width="200" height="24" aria-label="{k} history">
						{#if l.sim}<polyline points={l.sim} fill="none" stroke="var(--ink-500)" stroke-width="1" />{/if}
						<polyline points={l.live} fill="none" stroke="var(--secondary)" stroke-width="1" />
					</svg>
					<span>{num(last[k])}</span>
				</dd>
			{/each}
		</dl>
		<p class="dim">ticks {m.series[0].tick} to {last.tick}</p>
	{:else}
		<p class="dim">{m.series.length} ticks</p>
	{/if}

	{#each [{ label: 'forming', list: m.colonies.forming }, { label: 'living', list: m.colonies.living }] as { label, list } (label)}
		<h2>{label}</h2>
		{#if list.length}
			<ul class="plain">
				{#each list as c (c.id)}
					<li>
						<div class="row">
							<span>{name(c)}</span>
							{#if c.state === 'fading'}<span class="dim">fading</span>{/if}
							<span class="dim">{c.layer}</span>
							<span class="dim">{c.size}</span>
							<span class="dim">born {c.born_tick}</span>
							{#if c.fruited_tick !== null}<span class="dim">fruited {c.fruited_tick}</span>{/if}
						</div>
						<p class="members">
							{#each c.members as p (p.slug)}
								<a href="/i/{p.slug}" class:dim={!p.founder}>{p.slug}</a>{' '}
							{/each}
						</p>
					</li>
				{/each}
			</ul>
		{:else}
			<p class="dim">0 {label}</p>
		{/if}
	{/each}

	<h2>tunables</h2>
	<table class="tun">
		<thead><tr><th>name</th><th>value</th><th>default</th><th></th></tr></thead>
		<tbody>
			{#each m.tunables as t (t.name)}
				<tr>
					<td class:hot={draft[t.name] !== t.value}>{t.name}</td>
					<td>
						<input
							type="number"
							min={t.lo}
							max={t.hi}
							step={t.int ? 1 : 'any'}
							bind:value={draft[t.name]}
							oninput={() => (edited[t.name] = true)}
							class:bad={bad(t.lo, t.hi, t.int, draft[t.name])}
							aria-label={t.name}
						/>
					</td>
					<td class="dim" class:on={t.value !== t.default}>{t.default}</td>
					<td class="dim">{t.meaning}, {t.lo} to {t.hi}</td>
				</tr>
			{/each}
		</tbody>
	</table>
	<div class="row save">
		<input bind:value={versionNote} placeholder="note" aria-label="note" />
		<button disabled={busy || invalid || !changed.length} onclick={save}>save</button>
		{#if changed.length}
			<button class="link" onclick={() => load(true)}>undo</button>
		{/if}
		<span class="dim">version {m.tunables_version ?? 'none'}</span>
	</div>

	<h2>history</h2>
	{#if m.history.length}
		<table>
			<tbody>
				{#each m.history as h (h.version)}
					<tr>
						<td class="num">{h.version}</td>
						<td class="dim nowrap">{when(h.created_at)}</td>
						<td>{values(h.values)}</td>
						<td class="dim">{h.note ?? ''}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{:else}
		<p class="dim">0 versions</p>
	{/if}

	<h2>events</h2>
	{#if m.events.length}
		<table>
			<tbody>
				{#each m.events as e}
					<tr class:dim={e.simulated}>
						<td class="num">{e.tick}</td>
						<td class="nowrap">{when(e.scheduled_at)}</td>
						<td>{e.kind}</td>
						<td>{e.colony_id}{e.other_colony_id !== null ? ` ${e.other_colony_id}` : ''}</td>
						<td>{e.layer}</td>
						<td>{#if e.slug}<a href="/i/{e.slug}">{e.slug}</a>{/if}</td>
						<td>{e.simulated ? 'simulated' : ''}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{:else}
		<p class="dim">0 events</p>
	{/if}
{:else if note}
	<p class="dim">{note}</p>
{/if}

<style>
	.acts {
		margin-top: 1rem;
	}
	/* The layout's muted link colour outranks its own .hot, and an armed reset is one
	   click from a wipe. */
	.acts button.link.hot {
		color: var(--primary);
	}
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
		display: flex;
		align-items: center;
		gap: 1rem;
		font-variant-numeric: tabular-nums;
	}
	svg {
		flex: none;
		max-width: 100%;
	}
	.members {
		margin: 0.35rem 0 0;
		overflow-wrap: anywhere;
	}
	.members a {
		margin-right: 0.75rem;
	}
	.tun input {
		width: 6rem;
		font-variant-numeric: tabular-nums;
	}
	.tun input.bad {
		border-bottom-color: var(--primary);
	}
	.tun td.on {
		color: var(--foreground);
	}
	.save {
		margin-top: 1rem;
	}
	.nowrap {
		white-space: nowrap;
	}
	/* Tables carry more columns than a phone has; they scroll on their own. */
	table {
		display: block;
		overflow-x: auto;
	}
</style>
