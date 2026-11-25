# STATUS

- Owner: Aktan
- State: runs through obkit 0.5.6 with the `scoring` block of benchmark.yaml; public, validation (r9.13) and private (r9.9) sets locked; the right calls of a key list most of the fields and values its ticket asks for, checked against the vendor docs (about 14 keys still miss one); a search API's own failure that outlasts every attempt is its miss; every setup outside the validated 8 is `excluded` in tools/; the private run does not use the LLM verifier (`--verify`)
- Next step: the paid private run, on the setups the 17 and 20 Nov validation runs used (14 arms with modes, plus the no-search baseline; about 8 h at concurrency 16): `obkit go --set private --tools firecrawl,keenable_pro,keenable_realtime,nimble_standard,parallel_advanced_tool,parallel_fast_tool,tinyfish,you_highlights_core`
- Open decisions: the private run date and the setup freeze date
