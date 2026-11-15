import type { PageLoad } from './$types';
import { getGallery } from '../../../lib/api';

// An address that is not a gallery loads as null and the page says `not here`.
export const load: PageLoad = async ({ fetch, params }) => ({
	gallery: await getGallery(params.serial, fetch)
});
