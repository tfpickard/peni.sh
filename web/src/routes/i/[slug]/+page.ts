import { goto } from '$app/navigation';
import type { PageLoad } from './$types';
import type { ImageDetail, ImageRedirect } from '../../../lib/api';

export const load: PageLoad = async ({ fetch, params, url }) => {
	// The page's own ?sim is passed through so the neighbours match the mode being viewed.
	const sim = url.searchParams.get('sim');
	const q = sim ? `?sim=${encodeURIComponent(sim)}` : '';
	const res = await fetch(`/api/images/${encodeURIComponent(params.slug)}${q}`);
	if (res.status === 404) return { image: null };
	if (!res.ok) throw new Error(`image ${res.status}`);
	const body = (await res.json()) as ImageDetail | ImageRedirect;
	if ('redirect' in body) {
		await goto(`/i/${body.redirect}${q}`, { replaceState: true });
		return { image: null, redirected: true };
	}
	return { image: body };
};
