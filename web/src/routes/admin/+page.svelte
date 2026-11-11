<script lang="ts">
	import { onMount } from 'svelte';
	import * as admin from '../../lib/admin';

	type Row = {
		name: string;
		file: File;
		preview: string;
		progress: number;
		state: string;
		slug?: string;
		error?: string;
	};

	let authed = $state<boolean | null>(null);
	let password = $state('');
	let loginMsg = $state('');

	let rows = $state<Row[]>([]);
	let fields = $state<admin.Fields>({ caption: '', description: '', tags: '', prompt: '' });
	let busy = $state(false);
	let over = $state(false);
	let items = $state<admin.Recent[]>([]);

	const single = $derived(rows.length === 1);

	onMount(async () => {
		authed = await admin.me();
		if (authed) loadRecent();
	});

	async function loadRecent() {
		try {
			items = await admin.recent();
		} catch {
			items = [];
		}
	}

	async function enter(e: SubmitEvent) {
		e.preventDefault();
		const r = await admin.login(password);
		password = '';
		if (r === 'ok') {
			loginMsg = '';
			authed = true;
			loadRecent();
		} else loginMsg = r;
	}

	async function leave() {
		await admin.logout();
		authed = false;
	}

	function pick(files: FileList | File[] | null) {
		if (!files) return;
		for (const r of rows) URL.revokeObjectURL(r.preview);
		rows = [...files]
			.filter((f) => f.type.startsWith('image/'))
			.map((file) => ({
				name: file.name,
				file,
				preview: URL.createObjectURL(file),
				progress: 0,
				state: ''
			}));
	}

	function drop(e: DragEvent) {
		e.preventDefault();
		over = false;
		pick(e.dataTransfer?.files ?? null);
	}

	const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
	const settled = (s: string) => s === 'live' || s === 'failed';

	async function track(row: Row, sha256: string, stem: string) {
		while (true) {
			await sleep(2000);
			try {
				const s = await admin.status(sha256, stem);
				row.state = s.state;
				row.slug = s.slug;
				row.error = s.error;
				if (settled(s.state)) return;
			} catch {
				// A dropped poll is retried; the next one tells the truth.
			}
		}
	}

	async function post(e: SubmitEvent) {
		e.preventDefault();
		if (busy || !rows.length) return;
		busy = true;
		const text = single ? { ...fields } : null;
		const pending: Promise<void>[] = [];
		for (const row of rows) {
			try {
				row.state = 'uploading';
				const { sha256, stem } = await admin.upload(row.file, text, (f) => (row.progress = f));
				row.progress = 1;
				row.state = 'waiting';
				pending.push(track(row, sha256, stem));
			} catch (err) {
				row.state = 'failed';
				row.error = (err as Error).message;
			}
		}
		busy = false;
		fields = { caption: '', description: '', tags: '', prompt: '' };
		await Promise.all(pending);
		loadRecent();
	}
</script>

<svelte:head>
	<title>admin -- peni.sh</title>
	<meta name="robots" content="noindex" />
</svelte:head>

{#if authed === false}
	<form class="login" onsubmit={enter}>
		<input type="password" bind:value={password} aria-label="password" autocomplete="current-password" />
		<button>enter</button>
		{#if loginMsg}<span class="msg">{loginMsg}</span>{/if}
	</form>
{:else if authed}
	<form
		class="up"
		class:over
		onsubmit={post}
		ondragover={(e) => {
			e.preventDefault();
			over = true;
		}}
		ondragleave={() => (over = false)}
		ondrop={drop}
	>
		<input
			type="file"
			accept="image/png,image/jpeg,image/webp"
			multiple
			aria-label="image"
			onchange={(e) => pick(e.currentTarget.files)}
		/>

		{#if rows.length}
			<ul class="rows">
				{#each rows as row (row.preview)}
					<li>
						<img src={row.preview} alt="" />
						<span class="name">{row.name}</span>
						{#if row.state === 'uploading'}
							<progress value={row.progress} max="1"></progress>
						{:else if row.state === 'live' && row.slug}
							<a href="/i/{row.slug}">live</a>
						{:else if row.state}
							<span class:bad={row.state === 'failed'}>{row.state}</span>
							{#if row.error}<span class="err">{row.error}</span>{/if}
						{/if}
					</li>
				{/each}
			</ul>
		{/if}

		<input placeholder="caption" bind:value={fields.caption} disabled={!single} />
		<textarea placeholder="description" rows="3" bind:value={fields.description} disabled={!single}
		></textarea>
		<textarea placeholder="tags, one per line" rows="3" bind:value={fields.tags} disabled={!single}
		></textarea>
		<textarea placeholder="prompt" rows="3" bind:value={fields.prompt} disabled={!single}
		></textarea>

		<div class="actions">
			<button disabled={busy || !rows.length}>post</button>
			<button type="button" class="link" onclick={leave}>leave</button>
		</div>
	</form>

	<section class="recent">
		<h2>recent</h2>
		{#if items.length}
			<ul>
				{#each items as it}
					<li>
						{#if it.thumb}<img src={it.thumb} alt="" />{:else}<span class="blank"></span>{/if}
						{#if it.slug && it.status === 'live'}
							<a href="/i/{it.slug}">{it.slug}</a>
						{:else}
							<span>{it.slug ?? '-'}</span>
						{/if}
						<span class="dim">{it.status}</span>
					</li>
				{/each}
			</ul>
		{:else}
			<p class="dim">0 pictures</p>
		{/if}
	</section>
{/if}

<style>
	form {
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
		max-width: 32rem;
	}
	.login {
		flex-direction: row;
		align-items: center;
	}
	input,
	textarea {
		background: transparent;
		color: var(--foreground);
		border: 0;
		border-bottom: 1px solid var(--ink-700);
		font: inherit;
		padding: 0.4rem 0;
		resize: vertical;
	}
	input:disabled,
	textarea:disabled {
		opacity: 0.3;
	}
	input::placeholder,
	textarea::placeholder {
		color: var(--ink-500);
	}
	input[type='file'] {
		border: 0;
		color: var(--muted-foreground);
	}
	button {
		background: transparent;
		color: var(--primary);
		border: 0;
		font: inherit;
		padding: 0;
		cursor: pointer;
	}
	button:disabled {
		color: var(--ink-500);
		cursor: default;
	}
	.link {
		color: var(--muted-foreground);
	}
	.up {
		padding: 0.75rem;
		margin: -0.75rem;
		border: 1px dashed transparent;
	}
	.up.over {
		border-color: var(--secondary);
	}
	.actions {
		display: flex;
		gap: 1.5rem;
	}
	.rows,
	.recent ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	li {
		display: flex;
		align-items: center;
		gap: 0.75rem;
	}
	li img,
	.blank {
		width: 3rem;
		height: 3rem;
		object-fit: cover;
		flex: none;
	}
	.blank {
		background: var(--muted);
	}
	.name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		min-width: 0;
		color: var(--muted-foreground);
	}
	progress {
		width: 6rem;
		accent-color: var(--secondary);
	}
	.msg,
	.dim {
		color: var(--muted-foreground);
	}
	.bad,
	.err {
		color: var(--primary);
	}
	.err {
		font-size: 0.875rem;
		overflow-wrap: anywhere;
	}
	h2 {
		font-size: 1rem;
		font-weight: 400;
		color: var(--muted-foreground);
		margin: 3rem 0 0.75rem;
	}
</style>
