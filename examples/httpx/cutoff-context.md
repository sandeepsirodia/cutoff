# httpx: API changes that AI assistants get wrong

* The deprecated `proxies` argument has now been removed.
* The `proxy` argument was added. You should use the `proxy` argument instead of the deprecated `proxies`, or use `mounts=` for more complex configurations. (#2879)
* The `verify` argument as a string argument is now deprecated and will raise warnings.
- The per-request `cert`, `verify`, and `trust_env` arguments are escalated from raising errors if used, to no longer being available. These arguments should be used on a per-client instance instead, or in the top-level API.
- When using a client instance, the per-request usage of `verify`, `cert`, and `trust_env` have now escalated from raising a warning to raising an error. You should set these arguments on the client instead. (Pull #617)
