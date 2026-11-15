import { SvelteSet } from 'svelte/reactivity';

// Pictures opened this session. The server clears a picture's overgrowth on a view, but a
// grid restored from a snapshot (back, or a reload) still holds the old levels, so these
// render clear everywhere until the next fetch agrees.
const KEY = 'viewed';

function load(): string[] {
	try {
		const v: unknown = JSON.parse(sessionStorage.getItem(KEY) ?? '[]');
		return Array.isArray(v) ? v.filter((s): s is string => typeof s === 'string') : [];
	} catch {
		return [];
	}
}

export const viewed = new SvelteSet<string>(load());

export function markViewed(slug: string): void {
	if (viewed.has(slug)) return;
	viewed.add(slug);
	try {
		sessionStorage.setItem(KEY, JSON.stringify([...viewed]));
	} catch {
		// Private mode: the set lasts as long as the page does.
	}
}
