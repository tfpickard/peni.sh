<script lang="ts">
	import { onMount } from 'svelte';
	import { afterNavigate, goto } from '$app/navigation';
	import { page } from '$app/state';
	import * as admin from '../../lib/admin';
	import * as ops from '../../lib/admin-ops';

	let { children } = $props();

	let authed = $state<boolean | null>(null);
	let password = $state('');
	let loginMsg = $state('');
	let pending = $state(0);

	const items = ['pictures', 'comments', 'haikus', 'models', 'mycelium', 'queue', 'stats', 'system'];
	const here = $derived(page.url.pathname.split('/')[2] ?? '');

	async function count() {
		try {
			pending = (await ops.comments('pending')).length;
		} catch {
			pending = 0;
		}
	}

	onMount(async () => {
		authed = await admin.me();
	});

	afterNavigate(() => {
		if (authed) count();
	});

	$effect(() => {
		if (authed) count();
	});

	async function enter(e: SubmitEvent) {
		e.preventDefault();
		const r = await admin.login(password);
		password = '';
		if (r === 'ok') {
			loginMsg = '';
			authed = true;
		} else loginMsg = r;
	}

	async function leave() {
		await admin.logout();
		authed = false;
		await goto('/admin', { replaceState: true });
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
	<div class="admin">
		<nav>
			{#each items as it}
				<a href="/admin/{it}" class:on={here === it}>
					{it}{#if it === 'comments' && pending > 0}&nbsp;{pending}{/if}
				</a>
			{/each}
			<button class="link" onclick={leave}>leave</button>
		</nav>
		{@render children()}
	</div>
{/if}

<style>
	.login {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		max-width: 32rem;
	}
	nav {
		display: flex;
		flex-wrap: wrap;
		gap: 0.5rem 1.5rem;
		margin-bottom: 3rem;
	}
	nav a {
		color: var(--muted-foreground);
	}
	nav a.on {
		color: var(--foreground);
	}
	.msg {
		color: var(--muted-foreground);
	}

	/* Shared by every admin page so each one only carries its own layout. */
	.admin :global(input),
	.admin :global(textarea),
	.admin :global(select),
	.login input {
		background: transparent;
		color: var(--foreground);
		border: 0;
		border-bottom: 1px solid var(--ink-700);
		border-radius: 0;
		font: inherit;
		padding: 0.4rem 0;
	}
	.admin :global(select option) {
		background: var(--background);
	}
	.admin :global(input::placeholder) {
		color: var(--ink-500);
	}
	.admin :global(button),
	.login button {
		background: transparent;
		color: var(--primary);
		border: 0;
		font: inherit;
		padding: 0;
		cursor: pointer;
	}
	.admin :global(button:disabled) {
		color: var(--ink-500);
		cursor: default;
	}
	.admin :global(button.link),
	.admin :global(button.dim) {
		color: var(--muted-foreground);
	}
	.admin :global(button.link:disabled) {
		color: var(--ink-500);
	}
	.admin :global(button.on) {
		color: var(--foreground);
	}
	.admin :global(.dim) {
		color: var(--muted-foreground);
	}
	.admin :global(.hot) {
		color: var(--primary);
	}
	.admin :global(ul.plain) {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 1.25rem;
	}
	.admin :global(.row) {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.5rem 1.25rem;
	}
	.admin :global(.tabs) {
		display: flex;
		gap: 1.5rem;
		margin-bottom: 2rem;
	}
	.admin :global(.thumb) {
		width: 3rem;
		height: 3rem;
		object-fit: cover;
		flex: none;
		background: var(--muted);
	}
	.admin :global(table) {
		border-collapse: collapse;
		width: 100%;
	}
	.admin :global(th),
	.admin :global(td) {
		text-align: left;
		font-weight: 400;
		padding: 0.35rem 1.5rem 0.35rem 0;
		vertical-align: top;
	}
	.admin :global(th) {
		color: var(--muted-foreground);
	}
	.admin :global(h2) {
		font-size: 1rem;
		font-weight: 400;
		color: var(--muted-foreground);
		margin: 3rem 0 0.75rem;
	}
	.admin :global(.num) {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}
</style>
