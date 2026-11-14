<script lang="ts">
	import { onMount } from 'svelte';
	import * as ops from '../../../lib/admin-ops';

	const OTHER = '__other';
	const purposes = ['describe', 'haiku', 'grow', 'extract', 'name'] as const;
	type Purpose = (typeof purposes)[number];
	// The purposes with an effort key; the others always run at the model's own default.
	const withEffort: readonly Purpose[] = ['describe', 'extract'];
	const CAP = 'mycelium.daily_calls';

	type Block = {
		loaded: { model: string; effort: string; prompt: string };
		model: string;
		other: string;
		effort: string;
		prompt: string;
	};

	let data = $state<ops.Models | null>(null);
	let blocks = $state<Record<Purpose, Block> | null>(null);
	let note = $state('');
	let armed = $state('');
	let cap = $state(0);

	// Effort null means none; the select needs a string for it.
	const eff = (v: string | number | null) => (v === null ? 'none' : String(v));

	function block(m: ops.Models, p: Purpose): Block {
		const model = m.current[`${p}.model`] as string;
		const known = m.known_models.includes(model);
		const effort = withEffort.includes(p) ? eff(m.current[`${p}.effort`]) : 'none';
		const prompt = m.current[`${p}.prompt`] as string;
		return {
			loaded: { model, effort, prompt },
			model: known ? model : OTHER,
			other: known ? '' : model,
			effort,
			prompt
		};
	}

	// Reset is live whenever the stored value differs from the default, for every key.
	const custom = (key: string) => data!.current[key] !== data!.default[key];

	async function load() {
		data = await ops.models();
		blocks = {
			describe: block(data, 'describe'),
			haiku: block(data, 'haiku'),
			grow: block(data, 'grow'),
			extract: block(data, 'extract'),
			name: block(data, 'name')
		};
		cap = Number(data.current[CAP]);
	}

	onMount(() => {
		load().catch(() => (note = 'not here'));
	});

	const modelValue = (b: Block) => (b.model === OTHER ? b.other.trim() : b.model);

	async function run(fn: () => Promise<unknown>) {
		note = '';
		try {
			await fn();
			await load();
			note = 'saved';
		} catch {
			note = 'failed';
		}
	}

	const saveModel = (p: Purpose) => run(() => ops.putSetting(`${p}.model`, modelValue(blocks![p])));
	const resetModel = (p: Purpose) => run(() => ops.resetSetting(`${p}.model`));
	const saveEffort = (p: Purpose) => {
		const e = blocks![p].effort;
		return run(() => ops.putSetting(`${p}.effort`, e === 'none' ? null : e));
	};
	const resetEffort = (p: Purpose) => run(() => ops.resetSetting(`${p}.effort`));
	const capValid = $derived(Number.isInteger(cap) && cap >= 0 && cap <= 100000);
	const saveCap = () => run(() => ops.putSetting(CAP, cap));
	const resetCap = () => run(() => ops.resetSetting(CAP));
	const keyed = $derived(
		data?.keyed == null ? 'unknown' : data.keyed.length ? data.keyed.join(' ') : 'none'
	);
	const savePrompt = (p: Purpose) => run(() => ops.putSetting(`${p}.prompt`, blocks![p].prompt));
	const resetPrompt = (p: Purpose) => run(() => ops.resetSetting(`${p}.prompt`));

	async function sure(which: 'describe' | 'embed') {
		if (armed !== which) {
			armed = which;
			return;
		}
		armed = '';
		await run(which === 'describe' ? ops.redescribeAll : ops.reembedAll);
	}
</script>

{#if blocks && data}
	<dl>
		<dt>keyed</dt>
		<dd>{keyed}</dd>
	</dl>

	{#each purposes as p}
		{@const b = blocks[p]}
		<section>
			<h2>{p}</h2>
			<div class="row">
				<select bind:value={b.model} aria-label="{p} model">
					{#each data.known_models as m}<option value={m}>{m}</option>{/each}
					<option value={OTHER}>other</option>
				</select>
				{#if b.model === OTHER}
					<input bind:value={b.other} aria-label="{p} other model" placeholder="model id" />
				{/if}
				<button disabled={!modelValue(b) || modelValue(b) === b.loaded.model} onclick={() => saveModel(p)}>
					save
				</button>
				<button class="link" disabled={!custom(`${p}.model`)} onclick={() => resetModel(p)}>reset</button>
			</div>

			{#if withEffort.includes(p)}
				<div class="row gap">
					<select bind:value={b.effort} aria-label="{p} effort">
						{#each ['none', ...data.efforts] as e}<option value={e}>{e}</option>{/each}
					</select>
					<button disabled={b.effort === b.loaded.effort} onclick={() => saveEffort(p)}>save</button>
					<button class="link" disabled={!custom(`${p}.effort`)} onclick={() => resetEffort(p)}>
						reset
					</button>
				</div>
			{/if}

			<textarea class="mono" bind:value={b.prompt} rows="18" aria-label="{p} prompt" spellcheck="false"></textarea>
			<div class="row">
				<button disabled={b.prompt === b.loaded.prompt} onclick={() => savePrompt(p)}>save</button>
				<button class="link" disabled={!custom(`${p}.prompt`)} onclick={() => resetPrompt(p)}>
					reset
				</button>
			</div>
		</section>
	{/each}

	<section>
		<h2>daily calls</h2>
		<div class="row">
			<input type="number" min="0" max="100000" step="1" bind:value={cap} aria-label="daily calls" />
			<button disabled={!capValid || cap === data.current[CAP]} onclick={saveCap}>save</button>
			<button class="link" disabled={!custom(CAP)} onclick={resetCap}>reset</button>
		</div>
		<p class="dim">{data.calls_today} today</p>
	</section>

	<h2>all</h2>
	<div class="row">
		<button onclick={() => sure('describe')}>{armed === 'describe' ? 'sure' : 're-describe all'}</button>
		<button onclick={() => sure('embed')}>{armed === 'embed' ? 'sure' : 're-embed all'}</button>
		{#if armed}<button class="link" onclick={() => (armed = '')}>cancel</button>{/if}
	</div>
	{#if note}<p class="dim">{note}</p>{/if}
{:else if note}
	<p class="dim">{note}</p>
{/if}

<style>
	dl {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 0.25rem 2rem;
		margin: 0 0 1rem;
	}
	dt {
		color: var(--muted-foreground);
	}
	dd {
		margin: 0;
	}
	input[type='number'] {
		width: 8rem;
	}
	section {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
		max-width: 48rem;
	}
	.gap {
		margin-top: 0.25rem;
	}
	section textarea.mono {
		font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
		font-size: 0.875rem;
		line-height: 1.5;
		width: 100%;
		resize: vertical;
		border: 1px solid var(--ink-700);
		padding: 0.5rem;
	}
</style>
