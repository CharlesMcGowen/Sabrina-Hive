# IDOR Vulnerability Assessment

## Findings
- `/api/Users/:id`: Confirmed accessible. Sequential ID enumeration reveals user profile data.
- `/rest/basket/:id`: Confirmed accessible. Successfully retrieved basket contents for ID 1 and 2.

## Observations
- The API returns 500 errors for `/api/Baskets/:id` but 200 OK for `/rest/basket/:id`. This indicates the correct path for basket retrieval is `/rest/basket/`.
- Authorization is not enforced on these endpoints when a valid JWT is provided.

## Next Steps
- Use the `idor_enumeration_script.py` to map the full range of accessible baskets and user profiles.
- Attempt to modify basket contents using PUT/POST requests to the identified endpoints.