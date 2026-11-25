# Soluma for Bulwark

Soluma's themes and plugins for [Bulwark Webmail](https://bulwarkmail.org), published to
[Bulwark Extensions](https://extensions.bulwarkmail.org).

- `themes/`: one folder per theme, each with its own `manifest.json`.
- `plugins/`: one folder per plugin, each with its own `manifest.json`.

Each folder is its own listing in the directory, added with that folder as its subpath.
Versions are tagged per extension, `<id>-v<version>` (for example `soluma-sunrise-v1.3.0`).

`scripts/soluma-sunrise.py` checks Soluma Sunrise against Bulwark's install-time rules and writes
`dist/soluma-sunrise.zip` (`--check` to check only). It reads the Soluma mark and icon from a sibling
checkout of `soluma-io/design-kit` (`--brand DIR` to point elsewhere).
