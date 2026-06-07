"""Explore / Content Inspo — a gallery of ready-to-run creative workflows.

Each workflow is something we can actually deliver with the image (Imagen) and
video (Veo) tools plus the post/email skills. Clicking one launches its prompt
into a fresh chat session. Filterable by media (image/video) and by type.
"""

from __future__ import annotations

from typing import Any

# media: 'image' | 'video' ; type: a coarse content category for filtering.
WORKFLOWS: list[dict[str, Any]] = [
    {
        "key": "product_hero_image",
        "title": "Product hero shot",
        "media": "image", "type": "Product",
        "description": "A clean, on-brand hero image of a chosen product for the feed.",
        "prompt": "Generate an on-brand 1:1 hero image for our best-selling product "
                  "(look it up), then draft an Instagram post with caption and hashtags.",
    },
    {
        "key": "lifestyle_scene",
        "title": "Lifestyle scene",
        "media": "image", "type": "Lifestyle",
        "description": "Product shown in an aspirational real-world setting.",
        "prompt": "Generate a lifestyle image placing our product in an aspirational "
                  "real-world scene that fits our brand mood, in 4:5 for the feed.",
    },
    {
        "key": "carousel_concepts",
        "title": "3-concept carousel",
        "media": "image", "type": "Product",
        "description": "Three distinct image concepts for a single product to A/B.",
        "prompt": "Generate 3 distinct on-brand image concepts (n=3) for a product I'll "
                  "pick, so I can choose the strongest for a post.",
    },
    {
        "key": "promo_announcement",
        "title": "Promo announcement",
        "media": "image", "type": "Promotion",
        "description": "A bold sale/launch announcement graphic.",
        "prompt": "Generate a bold, on-brand promotional announcement image for an upcoming "
                  "sale, and draft matching Instagram caption copy.",
    },
    {
        "key": "story_image",
        "title": "Story (9:16) graphic",
        "media": "image", "type": "Story",
        "description": "A vertical 9:16 image sized for Stories.",
        "prompt": "Generate a 9:16 vertical on-brand Story image featuring a product and a "
                  "short hook overlay idea.",
    },
    {
        "key": "email_hero",
        "title": "Email hero + campaign",
        "media": "image", "type": "Email",
        "description": "A hero image plus a full launch email built around it.",
        "prompt": "Plan a launch email for a product I'll choose: generate a hero image, "
                  "write the copy, render the HTML, and show me a preview.",
    },
    {
        "key": "product_reveal_video",
        "title": "Product reveal video",
        "media": "video", "type": "Product",
        "description": "A short 16:9 product reveal clip for the feed.",
        "prompt": "Generate a short 16:9 on-brand product reveal video for a product I'll "
                  "pick, with smooth motion and a clean background.",
    },
    {
        "key": "story_video",
        "title": "Vertical story video",
        "media": "video", "type": "Story",
        "description": "A 9:16 vertical short for Stories/Reels.",
        "prompt": "Generate a 9:16 vertical short video showing our product in an "
                  "eye-catching, on-brand moment for Stories/Reels.",
    },
    {
        "key": "lifestyle_video",
        "title": "Lifestyle moment video",
        "media": "video", "type": "Lifestyle",
        "description": "Product in an atmospheric lifestyle clip.",
        "prompt": "Generate a short atmospheric lifestyle video placing our product in a "
                  "scene that matches our brand mood.",
    },
    {
        "key": "promo_teaser_video",
        "title": "Promo teaser video",
        "media": "video", "type": "Promotion",
        "description": "A punchy teaser for an upcoming drop or sale.",
        "prompt": "Generate a punchy short teaser video for an upcoming product drop or "
                  "sale, on-brand, and suggest a caption.",
    },
]

TYPES = ["Product", "Lifestyle", "Promotion", "Story", "Email"]


def all_workflows() -> list[dict[str, Any]]:
    return WORKFLOWS


def get(key: str) -> dict[str, Any] | None:
    return next((w for w in WORKFLOWS if w["key"] == key), None)
