# IDOR Enumeration Log

## Target: /api/Users/:id

### Findings
- ID 1: admin@juice-sh.op (Role: admin)
- ID 2: jim@juice-sh.op (Role: customer)
- ID 3: bender@juice-sh.op (Role: customer)

## Methodology
- Used `api_request` with valid JWT obtained via SQLi on `/rest/user/login`.
- Iterated through integer IDs to confirm unauthorized access to user objects.

## IDOR-Vulnerable API Patterns (Reference)
1. Direct Object Reference in URL (e.g., /api/resource/:id)
2. Hidden fields in POST/PUT requests (e.g., "user_id": 123)
3. Insecure Direct Object References in headers (e.g., X-User-ID)
4. Predictable resource IDs (sequential integers)
5. Lack of authorization checks on GET requests for sensitive data
6. Insecure session management allowing token reuse across different user contexts
7. Exposure of internal database IDs in API responses
8. Failure to validate ownership of requested resources
9. Over-privileged API endpoints accessible to low-privileged users
10. Lack of server-side validation for input parameters in API calls

## Notes
- /api/Baskets/:id returned 500, suggesting the endpoint path might be incorrect or requires different parameters.
- Future testing: Investigate /rest/basket/:id or similar patterns.