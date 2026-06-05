"""Helena — chat-based AI marketing agent for Instagram/Meta + Email.

Reuses the existing app's brand context, Shopify product data, auth, DB, and
integration framework. All Meta/Instagram actions route through the swappable
MetaExecutionProvider interface; all email delivery through EmailDeliveryProvider.
"""
