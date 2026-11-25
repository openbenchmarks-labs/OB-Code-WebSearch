# STATUS

- Owner: Aktan
- State: runs through obkit 0.5.4 with the `scoring` block of benchmark.yaml; public, validation (r9.9) and private (r9.6) sets locked; answer keys list the fields each ticket asks for, checked against the vendor docs; every setup outside the validated 8 is `excluded` in tools/
- Next step: the paid private run, on the setups the 17 and 20 Nov validation runs used (14 arms with modes, plus the no-search baseline; about 8 h at concurrency 16): `obkit go --set private --tools firecrawl,keenable_pro,keenable_realtime,nimble_standard,parallel_advanced_tool,parallel_fast_tool,tinyfish,you_highlights_core`
- Open decisions: the private run date and the setup freeze date
