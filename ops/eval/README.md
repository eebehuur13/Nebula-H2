# Eval cases

`nebula eval` checks known questions against Nebula without spending Claude tokens. For each case it reports where an answer goes missing:
- **extraction:** is the expected text in the file Nebula read?
- **retrieval:** does search, run as that person, return the file, and how high?
- **excerpt:** does the excerpt Claude would see contain the expected text?
- **access:** are files the person mustn't see kept out?
- **numbers:** does the SQL return the expected rows?

`cases.example.json` shows the format. A case names the question, who asks it (`as`), and what a good answer needs: the file's title and some text it should contain, files that must not come back (`must_not_see`), SQL with its expected rows, or `absent: true` when Nebula shouldn't have an answer. Absent-answer cases are marked "review", because only a person can judge them.

Write cases from real documents your organisation shares with Nebula. Import and index those documents first, and make sure the account in `as` can open them. Then run:

```sh
uv run nebula eval ops/eval/my-cases.json --out results.json
```

The harness doesn't judge Claude's final answers. `claude_checks.md` lists the checks that need a real conversation.
