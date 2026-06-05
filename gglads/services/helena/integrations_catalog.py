"""Static catalog for the Integrations page — three grouped sections of
platform cards. `real` marks the platforms with real functionality at launch;
the rest render as easy-to-wire "Connect" placeholders.

`auth` is how a card connects: 'oauth' (official OAuth we already have),
'browser_agent' (Meta-side platforms without push access yet — a human logs in
and the agent operates the session), or 'api_key' (token form).
"""

from __future__ import annotations

from typing import Any

# Platforms with real functionality at launch.
LAUNCH_REAL = {
    "instagram", "facebook_pages", "meta_ads", "shopify",
    "google_ads", "google_analytics", "google_flow",
}


def _card(key: str, name: str, info: str, auth: str = "browser_agent") -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "info": info,
        "auth": auth,
        "real": key in LAUNCH_REAL,
    }


SECTIONS: list[dict[str, Any]] = [
    {
        "key": "content_posting",
        "title": "Content Posting",
        "description": "Connect your social and content platforms to schedule and publish posts.",
        "cards": [
            _card("x_twitter", "X (Twitter)", "Schedule and publish posts to X."),
            _card("linkedin", "LinkedIn", "Publish updates to your LinkedIn page."),
            _card("youtube", "YouTube", "Manage video uploads and metadata.", "oauth"),
            _card("tiktok", "TikTok", "Publish short-form video content."),
            _card("instagram", "Instagram", "Publish and schedule Instagram posts; read Insights."),
            _card("facebook_pages", "Facebook Pages", "Publish to your Facebook Page; read engagement."),
            _card("pinterest", "Pinterest", "Create and schedule Pins."),
            _card("wordpress", "WordPress", "Publish blog posts to WordPress.com.", "oauth"),
            _card("wordpress_self", "WordPress (Self-Hosted)", "Publish to a self-hosted WordPress site.", "api_key"),
            _card("substack", "Substack", "Publish newsletter posts."),
            _card("webflow", "Webflow", "Publish CMS items to Webflow.", "oauth"),
            _card("ghost", "Ghost", "Publish posts to a Ghost site.", "api_key"),
            _card("framer", "Framer", "Publish content to Framer."),
            _card("threads", "Threads", "Publish posts to Threads."),
            _card("blogger", "Blogger", "Publish posts to Blogger.", "oauth"),
        ],
    },
    {
        "key": "channel_data",
        "title": "Channel Data & Setup",
        "description": "Connect your advertising and analytics platforms to track performance and manage campaigns.",
        "cards": [
            _card("shopify", "Shopify", "Sync products + push Shopify Email campaigns.", "oauth"),
            _card("google_analytics", "Google Analytics", "Read site traffic and conversion data.", "oauth"),
            _card("google_search_console", "Google Search Console", "Read search performance data.", "oauth"),
            _card("google_ads", "Google Ads", "Manage search campaigns and read performance.", "oauth"),
            _card("bing_ads", "Bing Ads", "Manage Microsoft Advertising campaigns.", "oauth"),
            _card("google_docs", "Google Docs", "Read and write campaign briefs.", "oauth"),
            _card("meta_ads", "Meta Ads", "Create and manage Meta ad campaigns; read metrics."),
            _card("tiktok_ads", "TikTok Ads", "Manage TikTok ad campaigns."),
            _card("klaviyo", "Klaviyo", "Sync email/SMS campaign data.", "api_key"),
            _card("mailchimp", "Mailchimp", "Sync email campaign data.", "oauth"),
            _card("instantly", "Instantly", "Manage cold-email campaigns.", "api_key"),
            _card("brevo", "Brevo", "Sync email/SMS data.", "api_key"),
            _card("beehiiv", "beehiiv", "Sync newsletter data.", "api_key"),
            _card("stripe", "Stripe", "Read revenue and subscription data.", "oauth"),
            _card("revenuecat", "RevenueCat", "Read subscription revenue data.", "api_key"),
            _card("notion", "Notion", "Read/write briefs and content calendars.", "oauth"),
            _card("airtable", "Airtable", "Sync content and campaign tables.", "oauth"),
            _card("omni_analytics", "Omni Analytics", "Read analytics data.", "api_key"),
            _card("hubspot", "HubSpot", "Sync CRM and marketing data.", "oauth"),
            _card("zoho_crm", "Zoho CRM", "Sync CRM data.", "oauth"),
            _card("apollo", "Apollo.io", "Sync prospecting data.", "api_key"),
            _card("posthog", "PostHog", "Read product analytics.", "api_key"),
            _card("semrush", "Semrush", "Read SEO/keyword data.", "api_key"),
            _card("github", "GitHub", "Link a repository.", "oauth"),
            _card("google_flow", "Google Flow", "Generate on-brand images (Imagen/Veo).", "oauth"),
        ],
    },
    {
        "key": "notifications",
        "title": "Notifications",
        "description": "Connect messaging apps to receive insights directly in your preferred channel.",
        "cards": [
            _card("slack", "Slack", "Receive performance insights in Slack.", "oauth"),
        ],
    },
]


def all_cards() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for section in SECTIONS:
        for card in section["cards"]:
            out[card["key"]] = card
    return out


def get_card(key: str) -> dict[str, Any] | None:
    return all_cards().get(key)
