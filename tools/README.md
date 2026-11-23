# tools

One `<tool>.yaml` file per search tool. Keeping each tool in its own file makes it easy to compare setups and open pull requests without merge conflicts.

Each file lists the tool's setups (`main`, `settings`, or `comparison`) and per-search list pricing (`list_usd`). A setup that runs with fetch also lists its per-fetch list price (`fetch_usd`), billed for each fetch the vendor answered without an error.

To add or update a tool: edit `tools/<tool>.yaml`, run the offline `selftest`, and open a pull request.
