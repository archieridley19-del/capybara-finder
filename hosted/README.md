# Hosted editions

Two single-file web apps that do the same job as the local Python tool, but use
a **grounded LLM** as the search engine instead of your own Serper key. Each is
one self-contained `.html` file — no build step, no server. Open it locally or
drop it on any static host.

Both keep the tool's core discipline: a niche only gets a score if the LLM's
response carries **real retrieval metadata** (Claude's `web_search_tool_result`
blocks / Gemini's `groundingMetadata`). No searches came back → the card says
**"couldn't verify"**, never a guessed number.

| File | Engine | Get a key | Notes |
|---|---|---|---|
| `gemini.html` | Gemini + Google Search grounding | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) (free) | CORS-friendly — hosts on any static site, e.g. **Antigravity** |
| `claude.html` | Claude + web search tool | [console.anthropic.com](https://console.anthropic.com) | Uses `anthropic-dangerous-direct-browser-access`; spends Claude credits |

## How to use

1. Open the file in a browser.
2. Paste your Gemini / Anthropic API key (stored only in your browser's
   localStorage — never sent anywhere but the API).
3. Paste your own niche ideas, one per line.
4. Hit **Research my list**. Each idea is searched and scored; results persist
   in the browser and can be filtered by verdict.

## Hosting it (for a friend, via Antigravity or any static host)

These are plain static files. Serve `gemini.html` from anywhere that serves
HTML and it works. The Gemini edition is the one to host — Google's API accepts
browser calls directly, so no backend is needed.

## The honest security caveat

The key lives in the page, so **anyone who can open the page can read the key**.
That's fine for you, or a friend running it on their own machine. For a truly
public site, move the key behind a tiny serverless function (e.g. an Antigravity
/ Cloud function) that holds the key and forwards the request, and have the page
call that instead of the LLM directly.

## Which to use

- **Gemini** — cheapest to run, easiest to host, no CORS fuss. Start here.
- **Claude** — if you prefer Claude's judgement and don't mind the credits.
- **The local Python tool** (`../capyfind`) — no LLM credits at all, uses your
  own Serper/Brave search key. Best for large batches.
