<script lang="ts">
	import { onMount } from 'svelte';
	import * as ops from '../../../lib/admin-ops';

	const tabs: ops.CommentStatus[] = ['pending', 'approved', 'rejected'];
	let tab = $state<ops.CommentStatus>('pending');
	let items = $state<ops.Comment[]>([]);
	let loaded = $state(false);

	async function load() {
		try {
			items = await ops.comments(tab);
		} catch {
			items = [];
		}
		loaded = true;
	}

	onMount(load);

	function pick(t: ops.CommentStatus) {
		tab = t;
		load();
	}

	async function set(c: ops.Comment, s: ops.CommentStatus) {
		await ops.setComment(c.id, s);
		load();
	}

	async function remove(c: ops.Comment) {
		await ops.deleteComment(c.id);
		load();
	}

	const moves: Record<ops.CommentStatus, ops.CommentStatus[]> = {
		pending: ['approved', 'rejected'],
		approved: ['rejected'],
		rejected: ['approved']
	};
	const verb = { approved: 'approve', rejected: 'reject', pending: 'pending' };
</script>

<div class="tabs">
	{#each tabs as t}
		<button class:on={tab === t} class:dim={tab !== t} onclick={() => pick(t)}>{t}</button>
	{/each}
</div>

{#if items.length}
	<ul class="plain">
		{#each items as c (c.id)}
			<li class="item">
				{#if c.thumb}<img class="thumb" src={c.thumb} alt="" />{:else}<span class="thumb"></span>{/if}
				<div class="main">
					<p class="body">{c.body}</p>
					<div class="row dim">
						<span>{c.name || 'anonymous'}</span>
						<span>{new Date(c.created_at).toISOString().slice(0, 10)}</span>
						<a href="/i/{c.slug}">{c.slug}</a>
					</div>
					<div class="row">
						{#each moves[tab] as m}
							<button onclick={() => set(c, m)}>{verb[m]}</button>
						{/each}
						<button class="link" onclick={() => remove(c)}>delete</button>
					</div>
				</div>
			</li>
		{/each}
	</ul>
{:else if loaded}
	<p class="dim">0 comments</p>
{/if}

<style>
	.item {
		display: flex;
		gap: 1rem;
	}
	.main {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		min-width: 0;
	}
	.body {
		margin: 0;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
</style>
