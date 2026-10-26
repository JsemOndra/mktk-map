# Security policy

mktk-map stores credentials for your MikroTik devices and can change their configuration, so security reports
are taken seriously. Note that the project is entirely AI-written and has not been reviewed by a human
(see the [README](README.md)).

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them privately via
[GitHub private vulnerability reporting](https://github.com/JsemOndra/mktk-map/security/advisories/new)
(the *Report a vulnerability* button on the Security tab).

Include what is affected, how to reproduce it and the possible impact. You will get a reply as soon as possible;
this is a hobby project, so please allow some time for a fix.

## Supported versions

Only the latest version on the `main` branch receives security fixes.

## Scope

In scope: the mktk-map application, its Docker setup and the documentation.
Out of scope: vulnerabilities in RouterOS itself (report those to MikroTik) and setups that expose mktk-map
to the internet, which is not supported.
