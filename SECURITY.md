# Security policy

## Supported version

Security fixes are applied to the latest release and the default branch. Older images and tags may not receive backports.

## Reporting a vulnerability

Please use [GitHub private vulnerability reporting](https://github.com/Herbertmt978/Subgen-English-Plex/security/advisories/new). Do not include credentials, media filenames, subtitle contents, Plex tokens, or private network details in a public issue.

## Deployment boundary

Subgen reads mounted media and writes subtitle files beside it. Keep the container on a trusted network, mount only the libraries it needs, leave `SUBGEN_BIND_ADDRESS=127.0.0.1` unless another trusted host must connect, and configure `SUBGEN_API_KEY` before exposing compute endpoints to a network.

Automatic media deletion is disabled in the public defaults. Enabling deletion is an operator decision and should only be done after reviewing the monitor's report-only output.
