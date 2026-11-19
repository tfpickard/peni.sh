import type { PageLoad } from './$types';
import { getCollection } from '../../../lib/api';

export const load: PageLoad = async ({ fetch, params }) => ({
	collection: await getCollection(params.slug, fetch)
});
