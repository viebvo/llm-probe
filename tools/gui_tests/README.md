# GUI regression

One command drives every tab of the web UI with [Playwright](https://playwright.dev/python/) against **mock model servers**
and a **copy of `results.db.sample`** - no real endpoint, no real data, nothing of yours is touched (it works in a temporary home
folder and starts its own server on a free port).

```bash
pip install playwright && playwright install chromium     # once
.venv/bin/python tools/gui_tests/run.py                    # everything (about 3-6 minutes)
.venv/bin/python tools/gui_tests/run.py --only results,test_tab
.venv/bin/python tools/gui_tests/run.py --update-baseline  # accept new numbers of the sample runs, on purpose
```

Exit code 0 = every check passed. **Run it after every build.**

| group | what it covers |
|---|---|
| `tour` | all tabs in light and dark: no script errors, the version, a tooltip text behind every `data-tip` |
| `home` | the Home tab (TL;DR lines, buttons) |
| `results` | the runs list (header check box, sort rotation, parameter columns), run details (thinking outcome, cost with currency, gauges, "show relevant prompts", greyed empty boxes), marking and export (host names masked) |
| `compare` | three runs side by side: legends with units, cost, highlight and pin |
| `responses` | the question above the answers, the pre-selection list and its presets, the text filter, column highlight |
| `prompts` | the catalogue, sort rotation, read-only built-in prompts |
| `settings` | endpoints, the currency and its unit next to the prices, no secret in the page, Test API |
| `test_tab` | the model list (size, quantization, sort, header check box), per-model parameters and the reset button, several thinking modes / num_ctx values = a batch, num_ctx typing (Ollama), read-only and greyed on vLLM, hidden without a known context, prompt presets |
| `security` | foreign Host header and foreign origin refused, security headers, no secret in the API answers |
| `numbers` | scores, single-phase statistics, answer checks and token totals of the sample runs against `baseline_sample.json` |
| `batch_thinking` | end to end on a mock model: two thinking modes ticked = two runs, each with its own mode |
| `live_output` | a fast model on a 4x throttled browser: responsive page, resize while streaming, tok/s, the stream open only while a test runs, the running dot, the text-only fade |

The mock servers (`mocks.py`): an Ollama look-alike with eight models (sizes, quantizations, capabilities), a vLLM look-alike
(`max_model_len` 32768) and an OpenAI-compatible model that streams fast, long answers.

**Every bug found in the GUI deserves one more check here** - write it in the group of the tab it belongs to. The suite is meant to
grow with the program; a green run says "the things we once got wrong still work".
