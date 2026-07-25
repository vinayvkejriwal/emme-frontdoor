# Emme Front Door

Emme Front Door is a mobile-first health-plan onboarding experience built for
the TOA Health Hack. It turns a daunting set of insurance questions into a
short, reassuring conversation that gives Emme the inputs needed to estimate a
member's real healthcare costs.

Members can start by uploading a Summary of Benefits and Coverage (SBC), an
Explanation of Benefits (EOB), or an image of their insurance card. The app
reads what it can, marks the source of every value, and lets the member correct
or complete the rest. They can also skip the upload immediately and enter
everything themselves.

> **Prototype and privacy notice:** Intake and submission records can include
> personal, financial, and health-plan information. This prototype stores those
> records locally; `sessions/` and `submissions/` are intentionally excluded
> from Git. Do not use it with real protected health information (PHI) until it
> has appropriate authentication, encryption, retention, access-control, and
> compliance safeguards.

## The problem

Plan-cost calculations are only as good as their inputs, but insurance forms
often make people feel lost before they have even started. The product focuses
on the *front door*: asking only what is useful, explaining each question in
plain language, and lowering friction by accepting documents people already
have.

An EOB is especially useful because it shows how a claim was processed: the
amount billed, applicable discounts, what the plan paid, the member's share,
and year-to-date deductible and out-of-pocket balances. See [UnitedHealthcare's
EOB overview](https://www.uhc.com/understanding-health-insurance/how-does-health-insurance-work/explanation-of-benefits)
and this [sample Blue Cross EOB](https://www.kentcountymi.gov/DocumentCenter/View/1459/BCBS-Understanding-Your-Explanation-of-Benefits-EOB-Statement-PDF).

## Member experience

The app is designed around two equally available paths:

1. **Upload and review.** The member uploads an SBC, EOB, insurance-card image,
   or other supported plan document. Available values are pre-filled, labelled
   as coming from a document, and presented for confirmation or editing.
2. **Enter details myself.** The member bypasses document upload with one tap
   and moves directly into the same editable review flow.

Both paths converge on a concise confirmation screen: **“Here’s what we know
about your plan.”** Members can see whether a value came from their document,
was entered by them, or is an estimate; fix a value inline; and download a PDF
summary.

### Interaction principles

- **Mobile first:** large touch targets, 16 px form text, and safe-area-aware
  layout for small screens.
- **Progressive disclosure:** low-sensitivity basics come before financial or
  health details.
- **Plain language:** each question includes why Emme needs the value and a
  practical hint for where to find it.
- **No dead ends:** a failed or partial extraction falls back to editable
  values; document upload is always optional.
- **Autosave:** partial answers persist locally so a member can resume after a
  refresh or interruption.
- **Transparent uncertainty:** every answer carries a source, confidence, and
  confirmation state so estimates are never presented as member-supplied facts.

## Data captured today

| Area | Current prototype coverage |
| --- | --- |
| Identity | Preferred name, email, ZIP code |
| Household | Household size, household income range, tax filing status |
| Plan details | Carrier, plan type (HMO/PPO/EPO/HDHP/POS), metal tier, primary-care doctor |
| Cost sharing | Individual and family deductibles, deductible met year-to-date, out-of-pocket maximum and amount met, monthly premium, common copays, coinsurance |
| Documents | Optional upload of up to three PDFs or supported images; an SBC and/or EOB can be uploaded together |
| HSA | HSA eligibility and current HSA balance |
| Prescriptions | Medication name, dosage, frequency, payment method, and preferred pharmacy |
| Upcoming care | Planned procedures and ongoing/chronic conditions |

The fields are defined centrally in [`schema.py`](schema.py). Each field stores
the value plus metadata used by downstream cost calculations:

```json
{
  "value": 1600,
  "source": "document",
  "confidence": 0.8,
  "confirmed": false,
  "sensitivity": "medium"
}
```

### Planned extensions

The hackathon brief also calls for plan name, HSA year-to-date and employer
contributions, pregnancy, and behavioral-health needs. These should be added as
separate fields before treating the prototype as a complete implementation of
the brief. Pregnancy and behavioral-health needs can currently be recorded as
free text under planned or ongoing care, but are not yet structured fields.

## Document extraction

The extraction pipeline intentionally has two layers:

1. **Local PDF pass:** `pypdf` reads text from uploaded PDFs and a regex layer
   looks for familiar plan and cost-sharing information. This works without an
   API key.
2. **Optional Claude pass:** when `ANTHROPIC_API_KEY` is configured, supported
   PDF and image uploads are also sent to Claude for structured extraction.
   Claude values take precedence when both passes find the same field.

Extraction is best-effort, never a source of truth. The UI always asks the
member to review populated values and gracefully continues with manual entry
when no fields can be read.

## Structured output

The app builds a structured JSON snapshot of the completed intake and sends it
to `POST /api/submit`. The export retains the complete schema as well as each
answer's value, source, confidence, confirmation, and skip state—ready for a
cost-calculation service to consume.

For development, the submitted JSON is saved under `submissions/` and the
member can download a one-page PDF plan summary. Those runtime folders are
local-only and ignored by Git.

## Run locally

Requires Python 3.9 or later.

```bash
git clone https://github.com/vinayvkejriwal/emme-frontdoor.git
cd emme-frontdoor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000) on your phone or
desktop browser.

### Enable optional Claude extraction

The local PDF pass works without a key. To add the Claude extraction pass,
either export the key in your shell:

```bash
export ANTHROPIC_API_KEY="your-key-here"
uvicorn main:app --reload
```

or add it to a local `.env` file and run:

```bash
uvicorn main:app --env-file .env --reload
```

Never commit `.env` files or API keys.

## Project structure

```text
main.py            FastAPI application, document extraction, persistence, PDF export
schema.py          Field definitions, guidance, sensitivity, defaults, validation
static/index.html  Responsive single-page member experience
requirements.txt   Python dependencies
sessions/          Local in-progress intake data (ignored by Git)
submissions/       Local submitted JSON records (ignored by Git)
```

## API surface

| Endpoint | Purpose |
| --- | --- |
| `GET /` | Serves the member-facing experience |
| `GET /api/schema` | Returns the intake schema and field guidance |
| `POST /api/extract` | Extracts values from document uploads |
| `POST /api/save` | Saves partial progress and returns a session ID |
| `GET /api/session/{session_id}` | Restores a saved intake |
| `POST /api/submit` | Persists the structured final submission |
| `GET /api/pdf/{session_id}` | Generates the member's PDF summary |

## Production considerations

This is an MVP, not a production healthcare system. Before handling real
member data, replace local file storage with an appropriately secured data
store; enforce authentication and authorization; encrypt data in transit and
at rest; establish retention/deletion policies; validate upload content more
strictly; and complete the required privacy, security, and regulatory review.

## License

Released under the [MIT License](LICENSE).
