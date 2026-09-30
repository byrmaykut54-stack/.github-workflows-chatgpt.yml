# NEXORA production security checklist

## Already enforced
- HTTP-only, Secure, SameSite session cookie
- Passwords stored with scrypt
- Login and registration rate limits
- Request body size limit
- Tenant-scoped business data
- Signed billing webhook support
- Signed iyzico subscription webhook support
- Security response headers
- WhatsApp access tokens are never returned by the business API

## Before public paid launch
- Configure real payment provider credentials and verify the complete checkout/callback flow.
- Configure SMTP credentials (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`) and `PUBLIC_APP_URL` for transactional email delivery.
- CSRF same-origin protection is implemented for cookie-authenticated state-changing requests.
- Encrypt WhatsApp access tokens at rest with a server-side key.
- Move rate limiting to shared storage for multi-instance deployments.
- Enable database backups and move off the temporary/free database before its expiry.
- Add server-side reminder delivery (Web Push/email/WhatsApp) rather than browser polling alone.
- Add Google Calendar OAuth only after the tenant and token-isolation tests pass.
- Review audit-log retention and access policy.
