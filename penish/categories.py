"""The gallery's categories, in the order the categories page lists them."""

from __future__ import annotations

# (slug, label, what belongs). The note is for the classifier, never shown.
CATEGORIES: tuple[tuple[str, str, str], ...] = (
    (
        "fine-art",
        "fine art",
        "painting in a traditional or academic manner: oils, tempera, "
        "watercolour, old-master and romantic styles, religious and history painting",
    ),
    (
        "modern-art",
        "modern art",
        "modernist, abstract, surrealist, expressionist, cubist, pop "
        "and other avant-garde painting",
    ),
    (
        "drawing",
        "drawing",
        "pencil, charcoal, ink or pastel work that is drawn rather than "
        "painted, including studies and sketches",
    ),
    (
        "printmaking",
        "printmaking",
        "woodcut, engraving, etching, lithograph, linocut, "
        "screenprint, or a picture made to look like one",
    ),
    (
        "illustration",
        "illustration",
        "book, editorial, scientific and children's illustration; field-guide plates and diagrams",
    ),
    ("cartoon", "cartoon", "gag cartoons, comics, caricature and animation styles"),
    (
        "poster",
        "poster",
        "posters, advertisements, record sleeves, packaging and other graphic "
        "design where lettering is part of the work",
    ),
    (
        "photography",
        "photography",
        "photographs made as pictures: portraits, still life, "
        "landscape, studio, fashion and art photography",
    ),
    (
        "photojournalism",
        "photojournalism",
        "photographs that report: news, documentary, "
        "street, events, crowds, archival and press photographs",
    ),
    (
        "digital-art",
        "digital art",
        "3d renders, cgi, pixel art and work that is plainly made "
        "on a computer rather than imitating another medium",
    ),
    ("collage", "collage", "collage, photomontage and mixed media"),
    (
        "sculpture",
        "sculpture",
        "sculpture, installation, ceramics and other objects, or a picture of one",
    ),
    ("folk-art", "folk art", "folk, naive and outsider art; devotional and vernacular objects"),
    (
        "erotica",
        "erotica",
        "nudity or sexual content that is the point of the picture, in any medium",
    ),
)

SLUGS: tuple[str, ...] = tuple(c[0] for c in CATEGORIES)
LABELS: dict[str, str] = {c[0]: c[1] for c in CATEGORIES}
MAX_PER_IMAGE = 2
