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

	const items = [
		'pictures',
		'comments',
		'haikus',
		'models',
		'mycelium',
		'collections',
		'queue',
		'stats',
		'system',
		'logs'
	];
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

	/* Shared by every admin page so each one only carries its own layout. The admin is
	   for one person on an iPad: a field has to look like a field before it is touched,
	   and 16px keeps Safari from zooming on focus. */
	.admin :global(input:not([type='checkbox']):not([type='radio']):not([type='file'])),
	.admin :global(textarea),
	.admin :global(select),
	.login input {
		box-sizing: border-box;
		background: var(--ink-900);
		color: var(--foreground);
		border: 1px solid var(--ink-500);
		border-radius: 0;
		font: inherit;
		font-size: max(1rem, 16px);
		line-height: 1.4;
		padding: 0.55rem 0.75rem;
		min-height: 44px;
	}
	.admin :global(textarea) {
		resize: vertical;
	}
	.admin :global(input:focus-visible),
	.admin :global(textarea:focus-visible),
	.admin :global(select:focus-visible),
	.login input:focus-visible {
		outline: 2px solid var(--secondary);
		outline-offset: 1px;
		border-color: var(--secondary);
	}
	.admin :global(input:disabled),
	.admin :global(textarea:disabled),
	.admin :global(select:disabled) {
		border-style: dashed;
		color: var(--muted-foreground);
		cursor: default;
	}
	.admin :global(select option) {
		background: var(--background);
	}
	.admin :global(input::placeholder),
	.admin :global(textarea::placeholder),
	.login input::placeholder {
		color: var(--ink-400);
	}
	/* A visible label above a field: placeholders vanish while typing. */
	.admin :global(.field) {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}
	.admin :global(.field > .l) {
		font-size: 0.875rem;
		color: var(--muted-foreground);
	}
	/* Checkboxes keep their small box; the label around them is the 44px target. */
	.admin :global(label.hit) {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 44px;
		min-height: 44px;
		cursor: pointer;
	}
	.admin :global(input[type='checkbox']) {
		width: 1.1rem;
		height: 1.1rem;
		margin: 0;
		accent-color: var(--primary);
		cursor: pointer;
	}
	/* Primary actions are real buttons; secondary and text actions stay bare. */
	.admin :global(button.go),
	.login button {
		box-sizing: border-box;
		background: transparent;
		font-family: inherit;
		cursor: pointer;
		min-height: 44px;
		padding: 0.55rem 1.4rem;
		border: 1px solid var(--primary);
		color: var(--primary);
		font-size: max(1rem, 16px);
	}
	.admin :global(button.go:hover:not(:disabled)) {
		background: var(--primary);
		color: var(--background);
	}
	.admin :global(button.go:disabled) {
		border-color: var(--ink-700);
		color: var(--ink-500);
	}
	.admin :global(button) {
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
