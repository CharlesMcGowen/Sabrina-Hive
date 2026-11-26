# Registration Flow Analysis

## Target
- URL: http://192.168.2.69:3000/#/register

## Form Elements
- emailControl: Email address
- passwordControl: Password (5-40 chars)
- repeatPasswordControl: Password confirmation
- securityAnswerControl: Security question answer

## Methodology
1. Test password policy enforcement (min/max length).
2. Test for SQLi in securityAnswerControl.
3. Observe API response for registration success/failure.

## Payloads
- SQLi: `' OR 1=1 --`
- XSS: `<script>alert(1)</script>`