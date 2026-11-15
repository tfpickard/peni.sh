<script lang="ts">
	import { onMount, untrack } from 'svelte';
	import { afterNavigate, goto } from '$app/navigation';
	import * as logs from '../../../lib/admin-logs';
	import * as ops from '../../../lib/admin-ops';

	const LEVEL = 'log.level';
	const KEEP = 'log.keep_days';
	const KEEP_MAX = 365;
	// Follow keeps this many of the newest lines; `more` pages back past them.
	const FOLLOW_MAX = 500;

	let f = $state<logs.Filters>(logs.noFilters());
	let items = $state<logs.Line[]>([]);
	let next = $state<number | null>(null);
	let dropped = $state(0);
	let levels = $state<string[]>(['debug', 'info', 'warning', 'error']);
	let loaded = $state(false);
	let loading = $state(false);
	let note = $state('');
	let follow = $state(false);
	let open = $state(new Set<number>());
	let details = $state<Record<number, logs.Detail | 'failed'>>({});

	type Kept = { level: string; keep: number; dLevel: string; dKeep: number };
	let kept = $state<Kept | null>(null);
	let levelDraft = $state('');
	let keepDraft = $state(0);
	let keptNote = $state('');

	// A newer query invalidates responses still in flight for an older one.
	let gen = 0;
	let timer: ReturnType<typeof setTimeout> | undefined;
	// The query this page last put in the URL, so its own writes are not read back as navigation.
	let written: string | null = null;

	function meta(p: logs.Page) {
		dropped = p.dropped;
		if (p.levels?.length) levels = p.levels;
	}

	async function reload() {
		const mine = ++gen;
		loading = true;
		try {
			const p = await logs.list(f);
			if (mine !== gen) return;
			items = p.items;
			next = p.next;
			note = '';
			meta(p);
		} catch {
			if (mine !== gen) return;
			items = [];
			next = null;
			note = 'failed';
		}
		loaded = true;
		loading = false;
	}

	async function more() {
		if (next === null || loading) return;
		const mine = gen;
		loading = true;
		try {
			const p = await logs.list(f, next);
			if (mine !== gen) return;
			items = [...items, ...p.items];
			next = p.next;
			note = '';
			meta(p);
		} catch {
			if (mine === gen) note = 'failed';
		} finally {
			if (mine === gen) loading = false;
		}
	}

	// Opened lines and fetched tracebacks for rows no longer in the list.
	function forget(list: logs.Line[]) {
		const ids = new Set(list.map((l) => l.id));
		for (const id of Object.keys(details).map(Number)) if (!ids.has(id)) delete details[id];
		if ([...open].some((id) => !ids.has(id))) open = new Set([...open].filter((id) => ids.has(id)));
	}

	// Only the newest FOLLOW_MAX lines stay; `next` moves up so `more` continues without a gap.
	function trim(list: logs.Line[]) {
		if (list.length <= FOLLOW_MAX) return list;
		const kept = list.slice(0, FOLLOW_MAX);
		next = kept[kept.length - 1].id;
		forget(kept);
		return kept;
	}

	async function poll() {
		if (loading) return;
		const mine = gen;
		const top = items[0]?.id ?? null;
		try {
			// Only what is newer than the top, so a quiet log costs an empty list.
			const p = await logs.list(f, null, logs.PAGE, top);
			if (mine !== gen) return;
			meta(p);
			// A full page of new lines may not reach the old ones; a gap in a log reads as
			// nothing happened, so start over from the newest instead.
			if (top === null || p.next !== null) {
				items = p.items;
				next = p.next;
				forget(items);
			} else if (p.items.length) items = trim([...p.items, ...items]);
			note = '';
		} catch {
			// A missed poll keeps the list; the next tick tries again.
		}
	}

	function apply() {
		clearTimeout(timer);
		const qs = logs.toQuery(f).toString();
		if (qs !== written) {
			written = qs;
			// Shallow: the filters are the page's own state, the URL only mirrors them.
			goto(qs ? `/admin/logs?${qs}` : '/admin/logs', { replace: true, shallow: true, reset: false });
		}
		open = new Set();
		reload();
	}

	function typed() {
		clearTimeout(timer);
		timer = setTimeout(apply, 300);
	}

	// Arrival, and links to this page from itself (job or picture in a line's context).
	afterNavigate((nav) => {
		const url = nav.to?.url;
		if (!url) return;
		const read = logs.fromQuery(url.searchParams);
		const qs = logs.toQuery(read).toString();
		if (qs === written) return;
		clearTimeout(timer);
		written = qs;
		f = read;
		open = new Set();
		reload();
	});

	async function loadKept() {
		const m = await ops.models();
		kept = {
			level: String(m.current[LEVEL]),
			keep: Number(m.current[KEEP]),
			dLevel: String(m.default[LEVEL]),
			dKeep: Number(m.default[KEEP])
		};
		levelDraft = kept.level;
		keepDraft = kept.keep;
	}

	async function setKept(fn: () => Promise<unknown>) {
		keptNote = '';
		try {
			await fn();
			await loadKept();
			keptNote = 'saved';
		} catch {
			keptNote = 'failed';
		}
	}

	const keepValid = $derived(Number.isInteger(keepDraft) && keepDraft >= 1 && keepDraft <= KEEP_MAX);

	onMount(() => {
		loadKept().catch(() => (kept = null));
		return () => clearTimeout(timer);
	});

	$effect(() => {
		if (!follow) return;
		// Only the toggle restarts the timer, not the filters or the list that poll reads.
		untrack(poll);
		const t = setInterval(() => {
			if (document.visibilityState === 'visible') poll();
		}, 5000);
		return () => clearInterval(t);
	});

	async function toggle(l: logs.Line) {
		const s = new Set(open);
		if (!s.delete(l.id)) s.add(l.id);
		open = s;
		if (!s.has(l.id) || !l.has_exc || details[l.id] && details[l.id] !== 'failed') return;
		try {
			details[l.id] = await logs.detail(l.id);
		} catch {
			details[l.id] = 'failed';
		}
	}

	const pad = (n: number) => String(n).padStart(2, '0');
	function when(iso: string) {
		const d = new Date(iso);
		return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
	}
	const firstLine = (s: string) => {
		const i = s.indexOf('\n');
		return i < 0 ? s : s.slice(0, i);
	};
	const tone = (level: string) =>
		level === 'error' || level === 'critical'
			? 'bad'
			: level === 'warning'
				? 'warn'
				: level === 'debug'
					? 'quiet'
					: '';
	const ctxInt = (c: logs.Context | null, k: string) => {
		const v = c?.[k];
		return typeof v === 'number' || (typeof v === 'string' && /^\d+$/.test(v)) ? String(v) : null;
	};
	const json = (c: logs.Context) => JSON.stringify(c, null, 2);
	const empty = (c: logs.Context | null): c is null => !c || Object.keys(c).length === 0;
</script>

{#if kept}
	<div class="kept">
		<div class="row">
			<span class="dim">kept</span>
			<select bind:value={levelDraft} aria-label="kept level">
				{#each levels as l}<option value={l}>{l}</option>{/each}
			</select>
			<button disabled={levelDraft === kept.level} onclick={() => setKept(() => ops.putSetting(LEVEL, levelDraft))}>
				save
			</button>
			<button
				class="link"
				disabled={kept.level === kept.dLevel}
				onclick={() => setKept(() => ops.resetSetting(LEVEL))}
			>
				reset
			</button>
		</div>
		<div class="row">
			<input type="number" min="1" max={KEEP_MAX} step="1" bind:value={keepDraft} aria-label="kept days" />
			<span class="dim">days</span>
			<button
				disabled={!keepValid || keepDraft === kept.keep}
				onclick={() => setKept(() => ops.putSetting(KEEP, keepDraft))}
			>
				save
			</button>
			<button class="link" disabled={kept.keep === kept.dKeep} onclick={() => setKept(() => ops.resetSetting(KEEP))}>
				reset
			</button>
			{#if keptNote}<span class="dim">{keptNote}</span>{/if}
		</div>
	</div>
{/if}

<div class="row filters">
	<select bind:value={f.level} onchange={apply} aria-label="level" class:unset={!f.level}>
		<option value="">level</option>
		{#each logs.FILTER_LEVELS as l}<option value={l}>{l}</option>{/each}
	</select>
	<select bind:value={f.source} onchange={apply} aria-label="source" class:unset={!f.source}>
		<option value="">source</option>
		{#each logs.SOURCES as s}<option value={s}>{s}</option>{/each}
	</select>
	<input class="q" bind:value={f.q} oninput={typed} placeholder="search" aria-label="search" />
	<input class="short" bind:value={f.logger} oninput={typed} placeholder="logger" aria-label="logger" />
	<input
		class="id"
		class:unset={logs.ignored(f.job)}
		bind:value={f.job}
		oninput={typed}
		placeholder="job"
		aria-label="job"
		inputmode="numeric"
	/>
	<input
		class="id"
		class:unset={logs.ignored(f.image)}
		bind:value={f.image}
		oninput={typed}
		placeholder="picture"
		aria-label="picture"
		inputmode="numeric"
	/>
	<button class="link" class:on={follow} aria-pressed={follow} onclick={() => (follow = !follow)}>follow</button>
	{#if dropped > 0}<span class="dim">{dropped} dropped</span>{/if}
</div>

{#if items.length}
	<ul class="lines">
		{#each items as l (l.id)}
			{@const d = details[l.id]}
			{@const job = ctxInt(l.context, 'job_id')}
			{@const pic = ctxInt(l.context, 'image_id')}
			<li>
				<button class="line" aria-expanded={open.has(l.id)} aria-controls="log-{l.id}" onclick={() => toggle(l)}>
					<span class="at">{when(l.at)}</span>
					<span class="lv {tone(l.level)}">{l.level}</span>
					<span class="dim">{l.source}</span>
					<span class="dim">{l.logger}</span>
					<span class="msg">{firstLine(l.message)}</span>
				</button>
				{#if open.has(l.id)}
					<div class="open" id="log-{l.id}">
						<div class="row dim">
							<span>{l.id}</span>
							<span>{l.at}</span>
							{#if job}<a href="/admin/logs?job={job}">job {job}</a>{/if}
							{#if pic}<a href="/admin/pictures/{pic}">picture {pic}</a>{/if}
						</div>
						<pre>{l.message}</pre>
						{#if l.has_exc}
							{#if d === 'failed'}
								<p class="dim">failed</p>
							{:else if d?.exc}
								<pre class="exc">{d.exc}</pre>
							{/if}
						{/if}
						{#if !empty(l.context)}
							<pre class="dim">{json(l.context)}</pre>
						{/if}
					</div>
				{/if}
			</li>
		{/each}
	</ul>
	{#if next !== null}
		<p><button class="link" disabled={loading} onclick={more}>more</button></p>
	{/if}
	{#if note}<p class="dim">{note}</p>{/if}
{:else if note}
	<p class="dim">{note}</p>
{:else if loaded}
	<p class="dim">0 lines</p>
{/if}

<style>
	.kept {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		margin-bottom: 2.5rem;
	}
	input[type='number'] {
		width: 5rem;
	}
	.filters {
		margin-bottom: 1.5rem;
	}
	.filters .unset {
		color: var(--ink-500);
	}
	.q {
		width: 14rem;
		max-width: 100%;
	}
	.short {
		width: 9rem;
	}
	.id {
		width: 5rem;
	}
	.lines {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	/* Beats the admin layout's shared button rule, which colours every button as an action. */
	.lines button.line {
		display: grid;
		grid-template-columns: 8.5rem 4.5rem 3.5rem minmax(0, 11rem) minmax(0, 1fr);
		gap: 0 1rem;
		width: 100%;
		padding: 0.2rem 0;
		text-align: left;
		color: var(--foreground);
		font-size: 0.875rem;
		font-variant-numeric: tabular-nums;
	}
	.line > span {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.at {
		color: var(--muted-foreground);
	}
	.lv.bad {
		color: var(--primary);
	}
	.lv.warn {
		color: var(--secondary);
	}
	.lv.quiet {
		color: var(--ink-500);
	}
	/* Narrow screens: the message gets a row of its own under the labels. */
	@media (max-width: 48rem) {
		.lines button.line {
			/* Fixed tracks, as in the wide layout, so the logger starts at the same x on every row. */
			grid-template-columns: 7.5rem 4rem 3.25rem minmax(0, 1fr);
			gap: 0 0.75rem;
			padding: 0.4rem 0;
		}
		.msg {
			grid-column: 1 / -1;
		}
	}
	.open {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
		margin: 0.5rem 0 1.5rem;
		min-width: 0;
		font-size: 0.875rem;
	}
	.open .row {
		gap: 0.25rem 1.25rem;
	}
	pre {
		margin: 0;
		font-family: ui-monospace, 'SF Mono', Menlo, monospace;
		font-size: 0.8125rem;
		line-height: 1.5;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	.exc {
		color: var(--ink-200);
	}
	p {
		margin: 1rem 0 0;
	}
</style>
