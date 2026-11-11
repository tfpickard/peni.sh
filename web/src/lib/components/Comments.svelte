<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { ApiError, getConfig, postComment, type Comment } from '../api';

	let { slug, comments }: { slug: string; comments: Comment[] } = $props();

	const SCRIPT = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';

	type Turnstile = {
		render: (el: HTMLElement, o: { sitekey: string; callback: (t: string) => void }) => string;
		reset: (id?: string) => void;
	};

	let name = $state('');
	let body = $state('');
	let note = $state('');
	let busy = $state(false);
	let token = $state<string | null>(null);
	let siteKey = $state<string | null>(null);
	let box = $state<HTMLElement>();
	let widget: string | undefined;

	const ts = () => (window as unknown as { turnstile?: Turnstile }).turnstile;

	function loadScript(): Promise<void> {
		return new Promise((resolve, reject) => {
			if (ts()) return resolve();
			const s = document.createElement('script');
			s.src = SCRIPT;
			s.async = true;
			s.onload = () => resolve();
			s.onerror = () => reject(new Error('turnstile'));
			document.head.append(s);
		});
	}

	onMount(async () => {
		try {
			siteKey = (await getConfig()).turnstile_site_key;
			if (!siteKey) return;
			await loadScript();
			// The box only exists once siteKey has rendered the markup.
			await tick();
			if (box && ts()) {
				widget = ts()!.render(box, { sitekey: siteKey, callback: (t) => (token = t) });
			}
		} catch {
			// A widget that never loads leaves the form unable to post, which is the safe side.
		}
	});

	async function submit(e: Event) {
		e.preventDefault();
		if (busy || !body.trim()) return;
		busy = true;
		note = '';
		try {
			await postComment(slug, { name: name.trim() || null, body, turnstile: token });
			name = body = '';
			note = 'held for review';
		} catch (err) {
			note = err instanceof ApiError && err.status === 429 ? 'wait' : 'no';
		} finally {
			// Tokens are single use, so the next post needs a fresh one.
			token = null;
			if (widget && ts()) ts()!.reset(widget);
			busy = false;
		}
	}
</script>

<section>
	<h2>comments</h2>
	{#each comments as c}
		<div class="c">
			<p>{c.body}</p>
			<span class="meta">{c.name ?? 'anonymous'} {c.created_at.slice(0, 10)}</span>
		</div>
	{/each}
	<form onsubmit={submit}>
		<input bind:value={name} placeholder="name" maxlength="40" autocomplete="off" />
		<textarea bind:value={body} rows="4" maxlength="2000" aria-label="comment"></textarea>
		{#if siteKey}<div bind:this={box}></div>{/if}
		<div class="row">
			<button disabled={busy}>post</button>
			{#if note}<span class="note">{note}</span>{/if}
		</div>
	</form>
</section>

<style>
	section {
		margin-top: 2.5rem;
		max-width: 40rem;
	}
	h2 {
		margin: 0 0 0.75rem;
		font-size: 0.8rem;
		font-weight: 400;
		color: var(--muted-foreground);
	}
	.c {
		margin: 0 0 1.25rem;
	}
	.c p {
		margin: 0;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	.meta,
	.note {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}
	form {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		margin-top: 1.5rem;
	}
	input,
	textarea {
		font: inherit;
		color: var(--foreground);
		background: none;
		border: 1px solid var(--muted);
		border-radius: 0;
		padding: 0.4rem 0.5rem;
		resize: vertical;
	}
	input {
		max-width: 16rem;
	}
	.row {
		display: flex;
		align-items: baseline;
		gap: 0.75rem;
	}
	button {
		padding: 0;
		border: 0;
		background: none;
		font: inherit;
		color: var(--primary);
		cursor: pointer;
	}
	button:disabled {
		color: var(--muted-foreground);
		cursor: default;
	}
</style>
