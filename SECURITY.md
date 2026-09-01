# Security policy

## Supported version

Security fixes are applied to the latest release and the default branch. Older images and tags may not receive backports.

## Reporting a vulnerability

Please use [GitHub private vulnerability reporting](https://github.com/Herbertmt978/Subgen-English-Plex/security/advisories/new). Do not include credentials, media filenames, subtitle contents, Plex tokens, or private network details in a public issue.

## Deployment boundary

Subgen reads mounted media and writes subtitle files beside it. Keep the container on a trusted network, mount only the libraries it needs, leave `SUBGEN_BIND_ADDRESS=127.0.0.1` unless another trusted host must connect, and configure `SUBGEN_API_KEY` before exposing compute endpoints to a network. Plex, Jellyfin, Emby, and Tautulli webhook routes remain unauthenticated and must stay inside that trusted boundary.

Automatic media deletion is disabled in the public defaults. When explicitly enabled, only the live monitor can delete an unchanged generation after typed FFprobe and isolated PyAV both conclusively classify it as invalid media; silent, indeterminate, inference, resource, OOM, pressure, native-crash, log-regex, and stale-replacement cases are retained. The repair utility is report/evidence-only, even when its legacy `delete` action is requested. Exact-file deletion requires Linux descriptor-relative filesystem operations and fails closed elsewhere. Keep monitor state on a local service-owned directory that is not group/world writable; never place it in the media tree or on an untrusted shared mount.
