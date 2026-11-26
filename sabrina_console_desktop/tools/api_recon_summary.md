# API Reconnaissance Summary

## Findings
- `/api/Users/:id`: Confirmed accessible. ID 1 (admin), ID 2 (jim), ID 3 (bender) retrieved.
- `/api/Baskets/:id`: Returns 500 Error. Likely incorrect path or requires different authentication/parameter structure.
- `/rest/user/login`: Vulnerable to SQLi (Auth Bypass). Confirmed via `' OR 1=1 --`.

## Methodology
- Used `api_request` to probe endpoints.
- Used JWT from SQLi to authenticate API calls.
- Identified that `/api/Baskets/:id` is not the correct endpoint for basket retrieval.

## Next Steps
- Investigate `/rest/basket/:id` or other variations for basket access.
- Continue IDOR testing on other user-related endpoints.
- Explore `/api/Products/` or similar to find valid endpoints.