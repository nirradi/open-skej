"""The ids of the former default booking target.

Stream 1 booked against one hardcoded string resource and user; once
``bookings.resource_id`` / ``bookings.user_id`` became real foreign keys onto
``resources`` and ``users``, those placeholders became the **integer ids of a
seeded default Resource and default user** — the row ``app.db.bootstrap``
plants. Task 4.11 deleted the unscoped route these ids backed, so nothing in
the running application reads them any more; only the test fixtures that still
call ``ensure_booking_defaults`` do (see that module's docstring for why the
plant is kept rather than unwound). Its id is ``1`` because it is inserted
with an explicit primary key by ``ensure_booking_defaults`` (see there).
"""

DEFAULT_USER_ID = 1
DEFAULT_RESOURCE_ID = 1
