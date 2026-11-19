import type { PageLoad } from './$types';
import { getBook } from '../../../../lib/api';

export const load: PageLoad = async ({ fetch, params }) => ({
	book: /^\d+$/.test(params.n) ? await getBook(params.slug, params.n, fetch) : null
});
