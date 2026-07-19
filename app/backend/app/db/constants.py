"""Placeholder identity constants.

Stream 1 bypasses authentication and multi-tenancy entirely: every booking belongs
to one hardcoded user on one hardcoded resource. Stream 2 owns real users and
Spaces and will replace these with values threaded through from the request.
"""

DEFAULT_USER_ID = "default-user"
DEFAULT_RESOURCE_ID = "default-resource"
