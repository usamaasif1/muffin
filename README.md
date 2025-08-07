#muffin
## 🌟 Vision & Road-map

The long-term goal is a browser-extension-flavoured web app that lets me spot, plan, and react to intraday moves without opening a heavyweight trading platform.  
I’m building it iteratively—below is the feature matrix.

| Status | Area | Details / Notes |
|--------|------|-----------------|
| **✅** | **Core chart** | Lightweight-Charts in a static HTML page; proxy candles through FastAPI. |
| **✅** | **Levels panel** | Live Min / Max / Avg / Mid for the visible dataset. |
| **🛠** | L.M.L / L.M.H | Add **L.M.L** (Last-Month-Low) & **L.M.H** (Last-Month-High) bands.  <br>Render as dotted horizontal lines & list the numeric value in the **Levels** panel. |
| **🛠** | P.P.M.L / P.P.M.H | Same idea, but for **Previous-Period-Month** low/high (i.e. June when we’re in July). |
| **✅** | Watch-list (basic) | Add ticker → button; list on the side; click = load chart. |
| **🛠** | Watch-list (pinned) | Persist to `localStorage`; drag-to-re-order; badge last %-move. |
| **🛠** | Stock search box | Auto-complete tickers via Polygon’s symbol endpoint. |
| **🛠** | Session filter | Checkbox already filters regular-hours candles in JS; add shaded PRE / POST ribbons like TV. |
| **🛠** | Strategy helpers | *Buy* / *Sell* / *Take-profit* / *Break-even* buttons that drop horizontal rays at the active price.  <br>Colour‐code (green/red/teal/grey). |
| **🛠** | Big-mover scan | Backend cron (or on-click) that returns symbols up **≥ 15 %** in any user-picked window (1 m → 1 h). |
| **🛠** | Alerts | Desktop notif or e-mail when a watch-list symbol trips the %-move rule. |
| **🛠** | Options chain | Quick modal pulling current OI + IV from Polygon options endpoint (stretch). |
| **🛠** | Deploy | Dockerfile + Fly.io free tier; auto-deploy on GitHub push. |

### Naming conventions

| Tag | Meaning |
|-----|---------|
| **L.M.L** | *Last-Month Low* (low of the trailing 30/31 d window) |
| **L.M.H** | *Last-Month High* |
| **P.P.M.L / P.P.M.H** | Low / High of the **previous** calendar month (not rolling) |

### Basic workflow once everything lands

1. **Add tickers** to the watch-list.  
2. App auto-loads the first symbol on start-up.  
3. Switch between **1 m • 15 m • 1 h • day • month** and eyeball the coloured level lines.  
4. Click **Buy / Sell** to mark intended entries; **Take-profit** or **Break-even** drop secondary lines.  
5. PRE / POST shading keeps me oriented around intraday gaps.  
6. Background scan yells when something moves > 15 % in the chosen timeframe— instant candidate for momentum plays.

---

> **Collaboration welcome!**  
> Feel free to open issues or PRs for any box that isn’t green yet. I’m keeping it framework-free on purpose—vanilla JS, FastAPI, and small focused modules.
