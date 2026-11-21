<script lang="ts">
	// The collection's record as a visitor reads it. The admin's preview draws this same
	// markup, and gives it `onedit` to put a pencil on each row.
	let {
		record,
		onedit
	}: { record: [string, string][]; onedit?: (index: number) => void } = $props();

	// A value may hold several lines (a provenance chain); each is shown as its own line.
	const lines = (v: string) =>
		v
			.split('\n')
			.map((x) => x.trim())
			.filter(Boolean);
</script>

{#if record.length}
	<dl class="record">
		{#each record as [label, value], i (i)}
			<!-- The server keeps no row with neither, so a draft's blank one is not shown. -->
			{#if label.trim() || value.trim()}
			<div>
				<dt>{label}</dt>
				<dd>
					{#each lines(value) as line}<span>{line}</span>{/each}
				</dd>
				{#if onedit}
					<button class="pencil" aria-label="edit {label || `row ${i + 1}`}" title="edit" onclick={() => onedit(i)}>
						<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
							<path d="M11 2.5 13.5 5 5.5 13H3v-2.5z" fill="none" stroke="currentColor" stroke-width="1.2" />
						</svg>
					</button>
				{/if}
			</div>
			{/if}
		{/each}
	</dl>
{/if}

<style>
	.record {
		margin: 0;
		max-width: 40rem;
	}
	.record div {
		display: grid;
		grid-template-columns: 7rem 1fr;
		gap: 0 1rem;
	}
	.record div + div {
		margin-top: 0.5rem;
	}
	dt {
		color: var(--muted-foreground);
		font-size: 0.8rem;
		line-height: 1.6rem;
	}
	dd {
		margin: 0;
	}
	dd span {
		display: block;
	}
	/* Narrow: the label sits over its value. */
	@media (max-width: 30rem) {
		.record div {
			grid-template-columns: 1fr;
		}
		.record div + div {
			margin-top: 0.75rem;
		}
	}
	/* Admin only: a pencil after the row, quiet until it is reached. */
	.record div:has(.pencil) {
		grid-template-columns: 7rem 1fr 2.75rem;
		align-items: start;
	}
	/* Under .record, so it outranks the admin's own bare-button colour. */
	.record .pencil {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 2.75rem;
		min-height: 2.75rem;
		margin: -0.6rem 0;
		color: var(--ink-500);
	}
	.record .pencil:hover,
	.record .pencil:focus-visible {
		color: var(--secondary);
	}
	@media (max-width: 30rem) {
		.record div:has(.pencil) {
			grid-template-columns: 1fr 2.75rem;
		}
		.record div:has(.pencil) dd {
			grid-column: 1;
		}
		.record .pencil {
			grid-column: 2;
			grid-row: 1 / span 2;
		}
	}
</style>
