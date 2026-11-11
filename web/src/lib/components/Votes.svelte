<script lang="ts">
	import { postVote, type Votes } from '../api';

	let { slug, votes }: { slug: string; votes: Votes } = $props();

	// Optimistic copy; null means the server value from the page load is current.
	let local = $state<Votes | null>(null);
	const v = $derived(local ?? votes);
	const net = $derived(v.net > 0 ? `+${v.net}` : String(v.net));

	function apply(base: Votes, value: -1 | 0 | 1): Votes {
		let { up, down } = base;
		if (base.mine === 1) up--;
		if (base.mine === -1) down--;
		if (value === 1) up++;
		if (value === -1) down++;
		return { up, down, net: up - down, mine: value };
	}

	async function cast(value: -1 | 1) {
		const before = v;
		// Clicking the active vote again withdraws it.
		const next = before.mine === value ? 0 : value;
		local = apply(before, next);
		try {
			local = await postVote(slug, next);
		} catch {
			local = before;
		}
	}
</script>

<div class="votes">
	<button class:on={v.mine === 1} onclick={() => cast(1)}>up</button>
	<span class="net">{net}</span>
	<button class:on={v.mine === -1} onclick={() => cast(-1)}>down</button>
	<span class="tally">{v.up} up {v.down} down</span>
</div>

<style>
	.votes {
		display: flex;
		align-items: baseline;
		gap: 0.75rem;
		margin: 0 0 1.5rem;
	}
	button {
		padding: 0;
		border: 0;
		background: none;
		font: inherit;
		color: var(--foreground);
		cursor: pointer;
	}
	button.on {
		color: var(--primary);
	}
	.tally {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}
</style>
