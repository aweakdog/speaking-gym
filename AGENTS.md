# Notes for coding agents

- The app lives in `speaking-gym/`: `server.py` (Python stdlib only, no third-party deps) + `app.js` / `style.css` (vanilla JS, no build step). There is no test suite.
- Verify changes with the same checks `deploy.sh` runs:
  `cd speaking-gym && node --check app.js && node --check data.js && node --check sw.js && python3 -c "import ast; ast.parse(open('server.py').read())"`
- Run locally: `SG_DATA_DIR=/tmp/sg-data python3 speaking-gym/server.py 8787` (or `speaking-gym/start.sh`, which uses the gitignored `speaking-gym/data/`). AI features need `deepseek_api_key` in `$SG_DATA_DIR/config.json`; never commit keys.
- After changing `app.js` / `style.css`, bump the asset version in `speaking-gym/index.html` (all four `vMMDDx` occurrences, e.g. `v1030a` → `v1030b`).
- Data files deployed with the code and reloaded by the server when their mtime changes: `topics.json` (from `data.js` via `tools/export_topics.js`), `freq_words.json` (`tools/build_freq_words.py`, from ECDICT), `freq_defs.json` (`tools/build_freq_defs.py`, generated with DeepSeek; resumable).
