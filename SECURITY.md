# Security policy

## Supported version

Security fixes are applied to the latest release and the default branch. Older images and tags may not receive backports.

## Reporting a vulnerability

Please use [GitHub private vulnerability reporting](https://github.com/Herbertmt978/Subgen-English-Plex/security/advisories/new). Do not include credentials, media filenames, subtitle contents, Plex tokens, or private network details in a public issue.

## Deployment boundary

Subgen reads mounted media and writes subtitle files beside it. Keep the container on a trusted network, mount only the libraries it needs, leave `SUBGEN_BIND_ADDRESS=127.0.0.1` unless another trusted host must connect, and configure `SUBGEN_API_KEY` before exposing compute endpoints to a network. Plex, Jellyfin, Emby, and Tautulli webhook routes remain unauthenticated and must stay inside that trusted boundary.

Automatic media deletion is disabled in the public defaults. Enabling deletion is an operator decision and should only be done after reviewing the monitor's report-only output. Exact-file deletion requires Linux descriptor-relative filesystem operations and fails closed elsewhere. Keep monitor state on a local service-owned directory that is not group/world writable; never place it in the media tree or on an untrusted shared mount.
