# Jarvis with ChaiUI

The local and Vercel web interface share the same generated static page.
Official registry source comes from https://ui.chaicode.com/r/{name}.json,
installed with the ChaiUI theme first through the shadcn CLI.

## Edit and build

```powershell
npm.cmd ci
npm.cmd run build:ui
npm.cmd run test:ui
python frontend/smoke.py
```

The optional visual smoke check requires Python Playwright and Microsoft Edge.
It uses synthetic API responses, no real provider keys, microphone or Gmail data.
Screenshots are saved under `screenshots/chaiui/` (gitignored).

- `template.html`: page structure, content and stable IDs for the application.
- `components/`: installed ChaiUI React component sources.
- `build.tsx`: renders the registry components to HTML at build time.
- `theme.css`: ChaiUI tokens, utilities and locally bundled fonts.
- `layout.css`: Jarvis-specific responsive layout and native control adapters.
- `../web/app.js`: runtime voice, documents, Gmail, keys and interaction state.

Commit regenerated `web/demo.html`, `web/chaiui.css`, `web/chaiui/` and `web/files/`.
The existing Vercel configuration deploys these assets without Node or React at
runtime. `python main.py` needs no frontend build once outputs are committed.
Do not edit generated `web/demo.html` directly or use the legacy dashboard exporter
`tools/build_web.py` for this interface.

## Component mapping

| Interface | ChaiUI implementation |
| --- | --- |
| Connect, send, voice, stop, navigation and settings actions | Button, including solid, outline and ghost variants |
| Settings, menu and close icons | IconButton |
| API key fields | Input |
| Provider, microphone, speaker and language pickers | Select (native select) |
| Message composer | Textarea within Card |
| Setup and current conversation | Card |
| Assistant status and attachment filename | Chip |
| Page artwork | Backdrop and supplied hexagon SVG |
| Settings and audio dialogs | Native dialog adapter with ChaiUI panel tokens; preserves showModal focus trapping and Escape |
| Read-aloud checkbox | Native input adapter with ChaiUI neutral styling; preserves checked/onchange semantics |
| Theme control | ChaiUI half-circle pattern with a single Appearance label |
| Conversation and activity lists | App-specific layout using ChaiUI surface, type and hairline tokens |

ChaiUI does not provide a voice engine or a chat transcript component. The actual
voice Button controls the existing tested audio code. Native dialog and checkbox
adapters avoid introducing a second client-side state owner alongside app.js.
All other listed registry primitives render their actual React implementation
at build time, rather than imitating their styles in CSS.

Checks cover dark/light at desktop and 375px, asset loading, no horizontal scroll,
provider entry, sending a message, navigation, settings, and audio dialogs. The
existing 58 browser regression tests also exercise provider and voice behavior.
