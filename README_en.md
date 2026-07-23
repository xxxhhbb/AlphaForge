# AlphaForge

**Autonomous Multi-Agent Alpha Research for China A-Shares**

> Thousands of hypotheses. One evidence standard. One actionable research answer.

![AlphaForge](frontend/public/brand/alphaforge-hero-v1.png)

AlphaForge turns a natural-language Alpha question into a bounded research campaign. Specialist research agents investigate approved data, design multiple hypotheses, write experiments, critique their work, and submit candidates. Isolated runners execute approved experiments, deterministic evidence services calculate the metrics, and the system delivers a professional research report.

[Chinese](README.md) · [Quick Start](docs/alphaforge/QUICKSTART.md) · [Architecture](docs/alphaforge/ARCHITECTURE.md) · [Examples](examples/questions.md) · [Contributing](CONTRIBUTING.md)

## What makes AlphaForge different

- **Research agents with real autonomy** — planning, data investigation, proxy selection, multi-experiment design, code generation, critique, and revision.
- **Six specialist perspectives** — fundamentals, expectations and events, market structure, alternative information, macro regimes, and portfolio incrementality.
- **Independent machine evidence** — research models propose and explain; deterministic services calculate IC, costs, turnover, stability, correlation, and incrementality.
- **A-share research semantics** — historical universes, trading calendars, data availability times, financial revisions, and event timing.
- **Reports that answer the question** — conclusions, tested candidates, research process, supporting and opposing evidence, data scope, and the next best experiment.

## Research flow

```text
Question
→ Research plan
→ Specialist agents
→ Read-only data and research tools
→ Multiple experiment candidates
→ Review gateway
→ Isolated research sandbox
→ Standard result tables
→ Deterministic evidence
→ Professional report
```

## Quick start

```powershell
git clone https://github.com/xxxhhbb/AlphaForge.git
Set-Location AlphaForge

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

alphaforge demo --offline --seed 7
```

Run the local product:

```powershell
Set-Location frontend
npm ci
npm run build
Set-Location ..

alphaforge serve --host 127.0.0.1 --port 8000
```

Run a research question with a configured model provider:

```powershell
alphaforge research "Does volume confirmation improve 50-day momentum for predicting 20-day A-share returns?" --label-horizon 20 --print-report
```

## Specialist research groups

| Group | Focus |
|---|---|
| Fundamental Research | Value, quality, growth, cash flow, and balance sheets |
| Expectations & Events | Earnings releases, forecasts, expectation changes, and event drift |
| Market Structure | Momentum, reversal, volatility, liquidity, volume, and costs |
| Alternative Information | News, reports, attention, flows, and auxiliary information |
| Macro Regimes | Rates, index states, volatility regimes, and risk appetite |
| Meta Alpha | Correlation, residual Alpha, portfolio gaps, and decay diagnosis |

## License

[MIT](LICENSE). Third-party notices and factor-source licenses are documented in [NOTICE](NOTICE) and the corresponding source directories.

## Acknowledgements

Thanks to Vibe-Trading, Qlib, and the open-source quantitative research and agent communities.
