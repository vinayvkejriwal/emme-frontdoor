"""Schema for the health-plan intake form.

Every answerable item is a *field object* carrying not just a value but the
provenance and trust metadata we need downstream: where it came from, how much
we believe it, whether a human confirmed it, and how sensitive it is.

Fields are grouped into three stages of escalating sensitivity so the intake
flow can ask for low-stakes information first and earn trust before asking
about income, health conditions, or prescriptions.
"""

import json
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1.1.0"

# --- source values ---
SOURCE_DOCUMENT = "document"   # parsed from an uploaded plan doc / SBC / EOB
SOURCE_MEMBER = "member"       # the member typed or selected it
SOURCE_ASSUMED = "assumed"     # inferred or defaulted by us
SOURCE_EMPTY = "empty"         # nothing known yet

SOURCES = (SOURCE_DOCUMENT, SOURCE_MEMBER, SOURCE_ASSUMED, SOURCE_EMPTY)

# --- sensitivity values ---
SENSITIVITY_LOW = "low"
SENSITIVITY_MEDIUM = "medium"
SENSITIVITY_HIGH = "high"

SENSITIVITIES = (SENSITIVITY_LOW, SENSITIVITY_MEDIUM, SENSITIVITY_HIGH)


def field(
    label,
    why_we_ask,
    sensitivity,
    value=None,
    source=SOURCE_EMPTY,
    confidence=0.0,
    confirmed=False,
    optional=False,
    placeholder=None,
    value_type="string",
    choices=None,
    item_schema=None,
    find_it=None,
    assumed_default=None,
    assumed_note=None,
    allow_other=False,
    searchable=False,
):
    # type: (...) -> Dict[str, Any]
    """Build one field object.

    An empty field carries confidence 0.0 -- confidence describes belief in the
    *value*, so it only becomes meaningful once a value exists.

    ``find_it`` is the one-line "where do I look for this" hint shown under the
    input. ``assumed_default`` backs the "Not sure" affordance: the value we
    fill in on the member's behalf, recorded with ``source="assumed"`` so
    nothing downstream mistakes it for something they told us.
    """
    if source not in SOURCES:
        raise ValueError("unknown source: %r" % (source,))
    if sensitivity not in SENSITIVITIES:
        raise ValueError("unknown sensitivity: %r" % (sensitivity,))
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be in [0, 1], got %r" % (confidence,))

    entry = {
        "value": value,
        "source": source,
        "confidence": confidence,
        "confirmed": confirmed,
        "sensitivity": sensitivity,
        "label": label,
        "why_we_ask": why_we_ask,
        "optional": optional,
        "value_type": value_type,
    }
    if placeholder is not None:
        entry["placeholder"] = placeholder
    if choices is not None:
        entry["choices"] = list(choices)
        entry["searchable"] = searchable
        entry["allow_other"] = allow_other
    if item_schema is not None:
        entry["item_schema"] = item_schema
    if find_it is not None:
        entry["find_it"] = find_it
    if assumed_default is not None:
        entry["assumed_default"] = assumed_default
    if assumed_note is not None:
        entry["assumed_note"] = assumed_note
    return entry


CARRIERS = [
    "Blue Cross Blue Shield",
    "UnitedHealthcare",
    "Aetna",
    "Cigna",
    "Kaiser Permanente",
    "Humana",
    "Anthem",
    "Molina Healthcare",
    "Centene",
    "Oscar Health",
    "Ambetter",
    "Highmark",
    "Independence Blue Cross",
    "Other",
]

PLAN_TYPES = ["HMO", "PPO", "EPO", "HDHP", "POS"]

METAL_TIERS = ["Bronze", "Silver", "Gold", "Platinum", "Catastrophic"]

# Stand-ins used when a member taps "Not sure". These are rough national
# midpoints for employer-sponsored coverage, not the member's real numbers --
# they exist so someone without their documents in hand can still get a
# directionally useful estimate. Anything filled from here is tagged
# source="assumed" and shown as "estimated" in the UI.
ASSUMED_MEDIANS_NOTE = "National median for employer plans"

# Premium stand-ins keyed on metal tier, since tier is the single best
# predictor we have before the member tells us anything. Falls back to
# PREMIUM_FALLBACK when the tier is unknown.
PREMIUM_BY_METAL_TIER = {
    "Bronze": 380,
    "Silver": 450,
    "Gold": 560,
    "Platinum": 700,
    "Catastrophic": 300,
}
PREMIUM_FALLBACK = 450

# The values the review screen pre-fills into anything still empty after
# extraction. Everything else falls back to its own ``assumed_default``.
SMART_DEFAULTS = {
    "deductible_individual": 1600,
    "deductible_family": 3200,
    "oop_max": 6000,
}


def smart_default(name, metal_tier=None):
    # type: (str, Optional[str]) -> Any
    """The value we pre-fill for ``name`` when nothing was extracted.

    ``monthly_premium`` keys off metal tier when we know it; everything else is
    a flat stand-in. Returns None when we have no defensible default -- email,
    ZIP, and carrier are never guessed.
    """
    if name == "monthly_premium":
        return PREMIUM_BY_METAL_TIER.get(metal_tier, PREMIUM_FALLBACK)
    if name in SMART_DEFAULTS:
        return SMART_DEFAULTS[name]
    return None

# Shape of one entry in the `prescriptions` list field.
PRESCRIPTION_ITEM_SCHEMA = {
    "drug": {"value_type": "string", "label": "Medication name"},
    "dosage": {"value_type": "string", "label": "Dosage (e.g. 10 mg)"},
    "frequency": {"value_type": "string", "label": "How often do you take it?"},
    "payment_method": {"value_type": "string", "label": "How do you pay for it?"},
    "pharmacy": {"value_type": "string", "label": "Which pharmacy fills it?"},
}


def stage_one():
    # type: () -> Dict[str, Dict[str, Any]]
    """Low-sensitivity basics: who you are and roughly what plan you hold."""
    return {
        "display_name": field(
            label="What should we call you?",
            why_we_ask=(
                "Only so the rest of this conversation feels less like a form. "
                "It never has to be your legal name, and you can skip it."
            ),
            sensitivity=SENSITIVITY_LOW,
            optional=True,
            placeholder="Anything you like",
        ),
        "email": field(
            label="What's your email address?",
            why_we_ask=(
                "So we can send you a copy of your plan summary and pick this "
                "back up if you close the tab."
            ),
            sensitivity=SENSITIVITY_LOW,
            value_type="email",
            find_it="Whichever address you'd actually check.",
        ),
        "zip": field(
            label="What's your ZIP code?",
            why_we_ask=(
                "Plan networks, prices, and available carriers are set "
                "county-by-county, so your ZIP determines which numbers apply "
                "to you."
            ),
            sensitivity=SENSITIVITY_LOW,
            value_type="zip",
            find_it="The ZIP where you live, not your employer's.",
        ),
        "household_size": field(
            label="How many people are in your household, including you?",
            why_we_ask=(
                "Household size decides whether family deductibles apply and "
                "is part of every subsidy and cost-sharing calculation."
            ),
            sensitivity=SENSITIVITY_LOW,
            value_type="integer",
            find_it="Everyone covered by the plan, including you.",
            assumed_default=1,
            assumed_note="Assuming just you",
        ),
        "carrier": field(
            label="Who is your insurance carrier?",
            why_we_ask=(
                "Your carrier tells us which plan documents and provider "
                "networks to read your costs from."
            ),
            sensitivity=SENSITIVITY_LOW,
            choices=CARRIERS,
            searchable=True,
            allow_other=True,
            find_it="Check the front of your insurance card.",
        ),
        "plan_type": field(
            label="What type of plan is it?",
            why_we_ask=(
                "HMO, PPO, EPO, and POS plans handle referrals and "
                "out-of-network care very differently -- this changes what "
                "you'll actually pay."
            ),
            sensitivity=SENSITIVITY_LOW,
            choices=PLAN_TYPES,
            searchable=True,
            find_it=(
                "Check the front of your insurance card -- usually printed "
                "right after the plan name."
            ),
            assumed_default="PPO",
            assumed_note="Most common plan type",
        ),
        "metal_tier": field(
            label="What metal tier is your plan?",
            why_we_ask=(
                "The tier is a shorthand for how costs split between you and "
                "the plan, and it helps us sanity-check the numbers you give "
                "us later."
            ),
            sensitivity=SENSITIVITY_LOW,
            choices=METAL_TIERS,
            searchable=True,
            find_it=(
                "On your plan summary or Marketplace confirmation. Employer "
                "plans often don't use tiers at all."
            ),
            assumed_default="Silver",
            assumed_note="Most commonly selected tier",
        ),
    }


def stage_two():
    # type: () -> Dict[str, Dict[str, Any]]
    """Medium-sensitivity plan mechanics: the actual cost-sharing numbers."""
    return {
        "deductible_individual": field(
            label="What's your individual deductible?",
            why_we_ask=(
                "This is the amount you pay before most coverage kicks in -- "
                "it's the single biggest driver of what a given year costs you."
            ),
            sensitivity=SENSITIVITY_MEDIUM,
            value_type="currency",
            find_it=(
                "Usually near the top of your EOB, or on your carrier's site "
                "under your plan summary."
            ),
            assumed_default=1600,
            assumed_note=ASSUMED_MEDIANS_NOTE,
        ),
        "deductible_family": field(
            label="What's your family deductible, if your plan has one?",
            why_we_ask=(
                "Families often hit a shared cap before every individual "
                "deductible is met, so we need both numbers to project costs."
            ),
            sensitivity=SENSITIVITY_MEDIUM,
            value_type="currency",
            optional=True,
            find_it=(
                "Listed beside the individual deductible on your plan "
                "summary, often as \"family\" or \"all covered members\"."
            ),
            assumed_default=3200,
            assumed_note=ASSUMED_MEDIANS_NOTE,
        ),
        "deductible_met_ytd": field(
            label="How much of your deductible have you met so far this year?",
            why_we_ask=(
                "Where you stand today decides what your next appointment "
                "costs -- not the number printed on your card."
            ),
            sensitivity=SENSITIVITY_MEDIUM,
            value_type="currency",
            find_it=(
                "Your carrier's site tracks this live -- look for "
                "\"deductible status\" or \"accumulators\"."
            ),
            assumed_default=0,
            assumed_note="Assuming nothing met yet this year",
        ),
        "oop_max": field(
            label="What's your out-of-pocket maximum?",
            why_we_ask=(
                "This is your worst-case ceiling for the year. Knowing it "
                "tells us how much risk you're actually carrying."
            ),
            sensitivity=SENSITIVITY_MEDIUM,
            value_type="currency",
            find_it=(
                "On your plan summary as \"out-of-pocket maximum\" or "
                "\"out-of-pocket limit\", just below the deductible."
            ),
            assumed_default=6000,
            assumed_note=ASSUMED_MEDIANS_NOTE,
        ),
        "oop_met_ytd": field(
            label="How much have you paid toward your out-of-pocket maximum "
                  "this year?",
            why_we_ask=(
                "If you're close to the ceiling, care for the rest of the year "
                "may be nearly free -- that can change when you schedule "
                "things."
            ),
            sensitivity=SENSITIVITY_MEDIUM,
            value_type="currency",
            find_it=(
                "Same place as your deductible status on your carrier's site, "
                "tracked as a running total."
            ),
            assumed_default=0,
            assumed_note="Assuming nothing paid yet this year",
        ),
        "monthly_premium": field(
            label="What do you pay in premium each month?",
            why_we_ask=(
                "Premiums are the cost you pay whether or not you use care, "
                "so they anchor any comparison between plans."
            ),
            sensitivity=SENSITIVITY_MEDIUM,
            value_type="currency",
            find_it=(
                "Your share only -- check a pay stub for the health insurance "
                "deduction, not the full premium your employer reports."
            ),
            assumed_default=PREMIUM_FALLBACK,
            assumed_note="Estimated from your metal tier",
        ),
        "copays": field(
            label="What are your copays for common visits?",
            why_we_ask=(
                "Copays are what you hand over at the desk. They tell us the "
                "real price of routine care like a primary-care or urgent-care "
                "visit."
            ),
            sensitivity=SENSITIVITY_MEDIUM,
            value_type="object",
            find_it=(
                "Printed on the front or back of your insurance card, and in "
                "the cost-sharing table of your plan summary."
            ),
            assumed_default={
                "primary_care": 25,
                "specialist": 50,
                "urgent_care": 60,
                "emergency_room": 350,
                "generic_rx": 10,
                "brand_rx": 45,
            },
            assumed_note="Typical copays for a mid-tier plan",
            item_schema={
                "primary_care": {"value_type": "currency",
                                 "label": "Primary care visit"},
                "specialist": {"value_type": "currency",
                               "label": "Specialist visit"},
                "urgent_care": {"value_type": "currency",
                                "label": "Urgent care"},
                "emergency_room": {"value_type": "currency",
                                   "label": "Emergency room"},
                "generic_rx": {"value_type": "currency",
                               "label": "Generic prescription"},
                "brand_rx": {"value_type": "currency",
                             "label": "Brand-name prescription"},
            },
        ),
        "coinsurance": field(
            label="What's your coinsurance rate after the deductible?",
            why_we_ask=(
                "Once your deductible is met you usually still owe a "
                "percentage. That percentage is what makes a big procedure "
                "expensive or manageable."
            ),
            sensitivity=SENSITIVITY_MEDIUM,
            value_type="percent",
            find_it=(
                "On your plan summary, usually written as \"you pay 20%\" "
                "after the deductible."
            ),
            assumed_default=20,
            assumed_note="Most common coinsurance rate",
        ),
    }


def stage_three():
    # type: () -> Dict[str, Dict[str, Any]]
    """High-sensitivity details: finances, health history, and care plans."""
    return {
        "income_range": field(
            label="Which range best describes your annual household income?",
            why_we_ask=(
                "Subsidies, cost-sharing reductions, and Medicaid eligibility "
                "are all income-based. A range is enough -- we don't need an "
                "exact figure."
            ),
            sensitivity=SENSITIVITY_HIGH,
            choices=[
                "Under $25,000",
                "$25,000-$49,999",
                "$50,000-$74,999",
                "$75,000-$99,999",
                "$100,000-$149,999",
                "$150,000-$199,999",
                "$200,000 or more",
                "Prefer not to say",
            ],
            find_it=(
                "Household income before taxes -- line 11 of last year's 1040 "
                "if you want to be exact."
            ),
        ),
        "filing_status": field(
            label="How do you file your taxes?",
            why_we_ask=(
                "Filing status changes the income thresholds used for "
                "subsidies and HSA limits, so the same income can produce "
                "different results."
            ),
            sensitivity=SENSITIVITY_HIGH,
            choices=[
                "Single",
                "Married filing jointly",
                "Married filing separately",
                "Head of household",
                "Qualifying surviving spouse",
                "Prefer not to say",
            ],
            find_it="The status on the top of last year's 1040.",
        ),
        "primary_doctor_name": field(
            label="Who is your primary care doctor?",
            why_we_ask=(
                "We check whether they're in network before recommending "
                "anything -- keeping your doctor is usually worth more than a "
                "small premium difference."
            ),
            sensitivity=SENSITIVITY_HIGH,
            optional=True,
            find_it="On your insurance card if your plan requires a PCP.",
        ),
        "prescriptions": field(
            label="Which prescriptions do you take regularly?",
            why_we_ask=(
                "Drug formularies differ enormously between plans. One "
                "medication on the wrong tier can outweigh every other cost "
                "difference."
            ),
            sensitivity=SENSITIVITY_HIGH,
            value=[],
            value_type="list",
            optional=True,
            find_it=(
                "Your pharmacy's app lists every active fill, dosage "
                "included."
            ),
            assumed_default=[],
            assumed_note="Assuming none",
            item_schema=PRESCRIPTION_ITEM_SCHEMA,
        ),
        "chronic_conditions": field(
            label="Do you manage any ongoing or chronic conditions?",
            why_we_ask=(
                "Ongoing conditions mean predictable, recurring care. That "
                "usually shifts the math toward richer coverage."
            ),
            sensitivity=SENSITIVITY_HIGH,
            value=[],
            value_type="list",
            optional=True,
            find_it=(
                "Anything you see a doctor for regularly -- diabetes, asthma, "
                "thyroid, blood pressure."
            ),
            assumed_default=[],
            assumed_note="Assuming none",
        ),
        "planned_procedures": field(
            label="Is there any care you're planning or expecting this year?",
            why_we_ask=(
                "A known surgery, birth, or course of treatment often means "
                "you'll hit your deductible -- which flips which plan is "
                "cheapest."
            ),
            sensitivity=SENSITIVITY_HIGH,
            value=[],
            value_type="list",
            optional=True,
            find_it=(
                "Surgeries, a birth, physical therapy, dental work -- anything "
                "already on the calendar or being discussed."
            ),
            assumed_default=[],
            assumed_note="Assuming none planned",
        ),
        "hsa_eligible": field(
            label="Are you eligible for a health savings account?",
            why_we_ask=(
                "An HSA changes the true cost of a high-deductible plan, "
                "because contributions come out of pre-tax income."
            ),
            sensitivity=SENSITIVITY_HIGH,
            value_type="boolean",
            find_it=(
                "If your plan is an HDHP, you're generally eligible. Your "
                "benefits portal will say so outright."
            ),
        ),
        "hsa_balance": field(
            label="What's your current HSA balance?",
            why_we_ask=(
                "Money already set aside is money you can spend on a "
                "deductible, so it lowers the real risk of a high-deductible "
                "plan."
            ),
            sensitivity=SENSITIVITY_HIGH,
            value_type="currency",
            optional=True,
            find_it="Your HSA administrator's app or dashboard.",
            assumed_default=0,
            assumed_note="Assuming no balance set aside",
        ),
    }


# How the confirmation screen groups fields for review. These cut across the
# three intake stages on purpose -- stages are about how much trust an answer
# requires, while these are about how a member thinks about their own plan.
# The four screens the member actually moves through. Screen 2 is one editable
# review page for everything we can extract or default; screen 3 holds only the
# things we genuinely cannot guess.
SCREEN_THREE_FIELDS = [
    "income_range",
    "primary_doctor_name",
    "prescriptions",
    "chronic_conditions",
    "planned_procedures",
]

# Fields nobody is asked outright. Each either has a safe stand-in or stays
# empty and shows as "Not answered" on the summary -- listed here so it's a
# recorded decision rather than an oversight.
UNPROMPTED_FIELDS = [
    "filing_status",        # no defensible default; ask later if needed
    "hsa_eligible",         # no defensible default
    "hsa_balance",          # defaults to no balance
]

REVIEW_GROUPS = [
    ("Identity", ["display_name", "email"]),
    ("Household", ["zip", "household_size", "income_range", "filing_status"]),
    ("Plan details", ["carrier", "plan_type", "metal_tier",
                      "primary_doctor_name"]),
    ("Cost-sharing", ["monthly_premium", "deductible_individual",
                      "deductible_family", "deductible_met_ytd", "oop_max",
                      "oop_met_ytd", "copays", "coinsurance"]),
    ("HSA", ["hsa_eligible", "hsa_balance"]),
    ("Prescriptions", ["prescriptions"]),
    ("Upcoming care", ["chronic_conditions", "planned_procedures"]),
]


STAGES = [
    {
        "number": 1,
        "key": "basics",
        "title": "The basics",
        "sensitivity": SENSITIVITY_LOW,
        "intro": "Just enough to know which plan rules apply to you.",
        "builder": stage_one,
    },
    {
        "number": 2,
        "key": "plan_costs",
        "title": "Your plan's costs",
        "sensitivity": SENSITIVITY_MEDIUM,
        "intro": (
            "These come straight off your plan documents. Upload them and "
            "we'll fill in what we can."
        ),
        "builder": stage_two,
    },
    {
        "number": 3,
        "key": "your_situation",
        "title": "Your situation",
        "sensitivity": SENSITIVITY_HIGH,
        "intro": (
            "The most personal part, and the part that most changes our "
            "advice. Skip anything you'd rather not share."
        ),
        "builder": stage_three,
    },
]


def build_schema():
    # type: () -> Dict[str, Any]
    """Return a fresh, fully empty intake structure."""
    stages = []
    for spec in STAGES:
        stages.append({
            "number": spec["number"],
            "key": spec["key"],
            "title": spec["title"],
            "sensitivity": spec["sensitivity"],
            "intro": spec["intro"],
            "fields": spec["builder"](),
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "enums": {
            "source": list(SOURCES),
            "sensitivity": list(SENSITIVITIES),
        },
        "field_contract": {
            "value": "The answer itself; null when unknown.",
            "source": "Where the value came from: %s." % ", ".join(SOURCES),
            "confidence": "0-1 belief in the value; 0 while empty.",
            "confirmed": "True once a human has explicitly signed off.",
            "sensitivity": "How guarded we should be when asking: %s."
                           % ", ".join(SENSITIVITIES),
            "label": "The question as shown to the member.",
            "why_we_ask": "Plain-language reason the member deserves to see.",
            "find_it": "Where to look for this value, if present.",
            "assumed_default": "Value used when the member taps \"Not sure\"; "
                               "recorded with source=\"assumed\".",
            "assumed_note": "Short explanation of that assumption.",
        },
        "screens": [
            {
                "number": 1,
                "key": "upload",
                "title": "Start with your documents",
            },
            {
                "number": 2,
                "key": "plan_details",
                "title": "Your plan details",
                "intro": (
                    "Here's what we've got. Tap any line to correct it -- "
                    "anything marked estimated is our guess, not your plan."
                ),
                "fields": (list(stages[0]["fields"].keys())
                           + list(stages[1]["fields"].keys())),
            },
            {
                "number": 3,
                "key": "about_you",
                "title": "A few things we can't guess",
                "intro": (
                    "These only come from you, and they change our advice more "
                    "than anything else here. Skip any of them."
                ),
                "fields": list(SCREEN_THREE_FIELDS),
            },
            {
                "number": 4,
                "key": "confirm",
                "title": "Here's what we know about your plan.",
            },
        ],
        "smart_defaults": {
            "flat": dict(SMART_DEFAULTS),
            "premium_by_metal_tier": dict(PREMIUM_BY_METAL_TIER),
            "premium_fallback": PREMIUM_FALLBACK,
            "never_guessed": ["email", "zip", "carrier", "display_name"],
        },
        "unprompted_fields": list(UNPROMPTED_FIELDS),
        "review_groups": [
            {"title": title, "fields": list(names)}
            for title, names in REVIEW_GROUPS
        ],
        "stages": stages,
    }


def check_review_groups():
    # type: () -> None
    """Fail loudly if a field is missing from the review screen or listed twice.

    The confirmation screen is the member's last look before submit, so a field
    silently absent from it is worse than a crash at import time.
    """
    all_fields = set()
    for spec in STAGES:
        all_fields.update(spec["builder"]().keys())

    grouped = []
    for _, names in REVIEW_GROUPS:
        grouped.extend(names)

    duplicated = sorted(n for n in set(grouped) if grouped.count(n) > 1)
    if duplicated:
        raise AssertionError("field(s) in more than one review group: %s"
                             % ", ".join(duplicated))

    missing = sorted(all_fields - set(grouped))
    if missing:
        raise AssertionError("field(s) missing from review groups: %s"
                             % ", ".join(missing))

    unknown = sorted(set(grouped) - all_fields)
    if unknown:
        raise AssertionError("review group(s) name unknown field(s): %s"
                             % ", ".join(unknown))


check_review_groups()


def iter_fields(schema=None):
    # type: (Optional[Dict[str, Any]]) -> List[Dict[str, Any]]
    """Flatten every field into ``(stage_key, name, field)`` records."""
    if schema is None:
        schema = build_schema()
    records = []
    for stage in schema["stages"]:
        for name, entry in stage["fields"].items():
            records.append({
                "stage": stage["key"],
                "name": name,
                "field": entry,
            })
    return records


def export_json(schema=None, indent=2):
    # type: (Optional[Dict[str, Any]], int) -> str
    """Serialize the whole structure as clean, stable JSON."""
    if schema is None:
        schema = build_schema()
    return json.dumps(schema, indent=indent, sort_keys=False,
                      ensure_ascii=False) + "\n"


if __name__ == "__main__":
    print(export_json(), end="")
