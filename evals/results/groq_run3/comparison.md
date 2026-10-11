# Partial run compared with eval run 3 on the same cases

Partial run model(s): openai/gpt-oss-120b. Cases where every configuration has a valid run: 12 (G-01, G-02, G-03, G-04, G-05, G-06, G-07, G-08, G-09, G-10, G-11, G-12). Quota-stopped runs excluded: Workflow + 1 LLM 1, Single (A) 1.

| Configuration | Partial run: passes | Run 3: mean passes per trial | Partial run: avg LLM calls | Partial run: avg tokens | Partial run: failed cases |
|---|---:|---:|---:|---:|---|
| Workflow + 1 LLM | 10/12 | 11.00/12 | 1.00 | 4536 | G-01, G-11 |
| Single (A) | 10/12 | 12.00/12 | 5.92 | 16255 | G-01, G-11 |
