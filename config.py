"""
Filter and scoring profile for the BD tracker.
Edit these lists as JMA's target NAICS codes / keywords / states change.
No logic lives here, just data.
"""

# NAICS codes JMA cares about
NAICS_CODES = [
    "541511",  # Custom Computer Programming Services
    "541512",  # Computer Systems Design Services
    "541519",  # Other Computer Related Services
    "541611",  # Administrative Management & General Management Consulting
    "541612",  # Human Resources Consulting Services
    "611430",  # Professional and Management Development Training
]

# Title keywords that pull in an opportunity even if NAICS doesn't match exactly
KEYWORDS_INCLUDE = [
    "program management",
    "software",
    "application support",
    "training",
    "FEPLA",
    "paid leave",
    "family leave",
    "HR",
    "workforce",
    "change management",
    "data engineering",
    "python",
]

# Title keywords that kill an opportunity outright, regardless of NAICS match
KEYWORDS_EXCLUDE = [
    "construction",
    "janitorial",
    "landscaping",
    "medical equipment",
]

# Set-aside codes JMA can legally pursue
ELIGIBLE_SETASIDES = ["WOSB", "EDWOSB", "SBA", "SBP", ""]  # "" = full & open

# Set-aside codes JMA is NOT eligible for as a prime
INELIGIBLE_SETASIDES = ["8A", "8AN", "HZC", "SDVOSBC", "SDVOSBS", "VSA", "VSS"]

# States that get a scoring boost (place of performance)
STATES_PRIORITY = ["MD", "DC", "VA"]

# Notice types worth an early-visibility scoring boost (human-readable strings
# as returned by the SAM.gov API's "type" field)
EARLY_VISIBILITY_TYPES = ["Sources Sought", "Presolicitation"]

# Max number of description fetches per run (rate-limit safety)
MAX_DESCRIPTION_FETCHES = 5

# Score threshold for "Review" status vs "Logged"
REVIEW_SCORE_THRESHOLD = 50

# Days-out window for the "approaching deadline" section of the digest email
DEADLINE_WARNING_DAYS = 5
