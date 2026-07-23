"""The default booking target for the unscoped booking routes.

Stream 1 booked against one hardcoded string resource and user. Now that
``bookings.resource_id`` and ``bookings.user_id`` are real foreign keys onto
``resources`` and ``users``, those placeholders are the **integer ids of the
seeded default Resource and default user** instead — the row ``app.db.bootstrap``
plants so the still-unauthenticated routes have a valid target to book against.

These are transitional. Stream 4 adds Resource-scoped booking routes that pass a
real Resource and the authenticated caller straight through, and then deletes the
unscoped routes — at which point nothing reads these constants. Until then the
default row is guaranteed to exist wherever the unscoped routes run: the test
suite seeds it in the ``driver`` fixture, and the sandbox seeds it after
migrating. Its id is ``1`` because it is inserted with an explicit primary key by
``ensure_booking_defaults`` (see there).
"""

DEFAULT_USER_ID = 1
DEFAULT_RESOURCE_ID = 1
