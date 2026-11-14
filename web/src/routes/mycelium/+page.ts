import type { PageLoad } from './$types';
import { getMap } from '../../lib/api';

// A failed fetch draws as an empty map rather than an error page.
export const load: PageLoad = async ({ fetch }) => ({
	map: await getMap(fetch).catch(() => null)
});
