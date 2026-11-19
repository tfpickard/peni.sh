import type { PageLoad } from './$types';
import { getBook } from '../../../../lib/api';

export const load: PageLoad = async ({ fetch, params }) => ({
	book: await getBook(params.slug, params.n, fetch),
	slug: params.slug
});
