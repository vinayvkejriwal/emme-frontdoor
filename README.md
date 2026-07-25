# Emme Front Door

A mobile-first health-plan intake experience. Members can upload plan documents,
review extracted and estimated coverage details, answer a small set of follow-up
questions, and download a confirmation PDF.

> **Privacy note:** This is a prototype. Session and submission data can contain
> personal or health-plan information, so the app stores them locally and Git
> intentionally ignores the `sessions/` and `submissions/` folders. Do not use
> this app with real protected health information without suitable security,
> privacy, and compliance controls.

## What it does

- Guides a member through a short, mobile-friendly intake flow
- Accepts plan-document uploads (PDF and supported images)
- Extracts available plan details locally from PDFs, with an optional Claude
  extraction pass when an Anthropic API key is configured
- Clearly labels information as supplied by the member, read from a document,
  or estimated
- Saves progress locally and produces a downloadable confirmation PDF

## Run locally

This project requires Python 3.9 or later.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

## Optional document extraction with Claude

The app can still do basic local PDF extraction without an API key. To enable
the optional Claude extraction pass, create a local `.env` file or set the
environment variable before running the app:

```bash
export ANTHROPIC_API_KEY="your-key-here"
uvicorn main:app --reload
```

Never commit `.env` or API keys. They are already excluded from Git.

## Project layout

```text
main.py            FastAPI application and extraction/PDF endpoints
schema.py          Intake fields, stages, and validation metadata
static/index.html  Single-page mobile web interface
sessions/          Local in-progress intake data (ignored by Git)
submissions/       Local submitted intake data (ignored by Git)
```

## Publish with VS Code

1. Open this folder in VS Code: **File → Open Folder…**
2. Open **Source Control** in the left sidebar.
3. Verify that `README.md` and `.gitignore` are the changes shown. The local
   `sessions/` files should not appear.
4. Stage the files with the **+** button, enter a message such as
   `Add project README and ignore local data`, then click **Commit**.
5. Click **Publish Branch**. Sign in to GitHub if VS Code asks, choose a
   repository name, and select whether it should be private or public.

For this prototype, choose a **private** repository unless you have reviewed
the code and history for sensitive information.

## License

Released under the [MIT License](LICENSE).
