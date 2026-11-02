# Disputable

An agent that decides which chargebacks to fight. Built solo for the Airwallex Agentic Banking Hackathon (Starter Kit 04 – Dispute Response Agent), on the Airwallex sandbox.

> Status: early build. The idea is in [docs/idea/idea-submission.md](docs/idea/idea-submission.md).

## What it does

Disputable works through an online store's chargeback queue and decides, case by case, whether to accept, fight with evidence, or hand the case to a person. My code makes every money decision. Claude only reads emails and records and suggests next steps. When the bank pushes back, the agent decides again with new evidence instead of repeating itself.

## Setup

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Then create a file named `.env` in the project folder with your Airwallex **sandbox** Client ID and API key:

```
AWX_CLIENT_ID=your client id
AWX_API_KEY=your api key
```

Never commit `.env`. [docs/getting-api-keys.md](docs/getting-api-keys.md) explains how to get the keys.

## Sandbox spike

`scripts/spike.py` runs the real dispute flow against the Airwallex sandbox and records every request and response in `fixtures/spike/`, with secrets, browser details and the account ID removed. The store, customer and orders in them are made up.

```bash
uv run python scripts/spike.py all
```

The sandbox doesn't let this account send card numbers from a server, so the `payments` step pays with Airwallex's test card inside the Airwallex.js Card element, on a local page in a hidden browser window. It needs Microsoft Edge or Google Chrome.

You can also run one step at a time (`login`, `payments`, `disputes`, ... see `--help`). `report` prints what the API answered without making any calls. Fields the docs leave unclear are guesses in one block of constants at the top of the script, and the report shows which guesses the sandbox accepted.

## Tests

```bash
uv run pytest
```
