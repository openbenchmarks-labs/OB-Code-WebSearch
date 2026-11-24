# tools

One `<tool>.yaml` file per search tool. Keeping each tool in its own file makes it easy to compare setups and open pull requests without merge conflicts.

Each file lists the tool's setups (`main`, `settings`, or `comparison`) and per-search list pricing (`list_usd`). A setup that runs with fetch also lists its per-fetch list price (`fetch_usd`), billed for each fetch the vendor answered without an error.

`pricing.answer_usd` is what one answer costs at the vendor, which obkit adds to the model cost in `harness/config.yaml` to size a run. `expected` is the list price times the searches and fetches a saved validation run billed per answer; a setup with no saved run counts 5 searches, and 5 fetches in its fetch mode. Either way it is averaged over the setup's modes. `max` is the most the harness limits let an answer bill: `max_turns` searches when the vendor bills empty results (`bills_empty: true`), else `max_searches`, plus `max_fetches` fetches.

To add or update a tool: edit `tools/<tool>.yaml`, run the offline `selftest`, and open a pull request.
