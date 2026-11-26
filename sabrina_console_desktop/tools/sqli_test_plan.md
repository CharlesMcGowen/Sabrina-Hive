# SQLi Test Plan
Target: /rest/user/login
Payloads: [' OR 1=1 --, admin'--]
Goal: Bypass authentication or trigger error-based feedback.