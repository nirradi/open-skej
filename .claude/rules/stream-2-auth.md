# Stream 2: Auth, Access & Admin (The "Gatekeepers")

## Objective
Implement the multi-tenant access logic, Admin configuration dashboard, and real database provisioning.

## Boundaries & Constraints
* This stream is strictly responsible for User and Space relationships.
* Do not touch the actual calendar or booking mechanics.
* Owns the initial provisioning of the real database (e.g., PostgreSQL).

## Database Responsibilities
* Provision the primary database schema.
* Define tables for `Users`, `Spaces` (Resources), and access permissions.

## Backend Responsibilities
* Integrate Auth0 middleware to verify JWTs securely.
* Create endpoints for Admins to create new "Spaces".
* Create endpoints for Admins to invite and approve users to a Space.
* Create endpoints for Users to request access to a Space.

## Frontend Responsibilities
* Implement the Auth0 React SDK for login/logout flows.
* Build the Admin dashboard for Space creation and user management (approving/inviting).
* Build the User view to request access to unapproved spaces.