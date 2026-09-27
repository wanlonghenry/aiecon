# aiecon

> Reconstruct AI workflow costs, reconcile them with provider reports, and inspect evidence-backed waste and context reuse opportunities. Run the local demo without API keys.

**Status: prototype under construction.** This repository is being built task by task from [`PLAN.md`](PLAN.md). Commands and interfaces listed there are conventions to implement, not claims about what already works. Nothing here has been validated against real provider costs yet.

## Planned quickstart

```bash
git clone https://github.com/wanlonghenry/aiecon.git
cd aiecon
uv sync --locked
uv run aiecon demo --out .aiecon/demo
```

The demo is designed to run fully offline with synthetic fixtures. No API key, no network, no second repository.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
