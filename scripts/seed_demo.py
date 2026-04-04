"""
scripts/seed_demo.py — Seed jobs.demo.db with realistic fake listings for demo mode.

Drops and recreates jobs.demo.db from scratch, then inserts 18 fictional listings
across three score tiers (high / mid / low). Run this before launching the app
with --demo:

    .venv/Scripts/python scripts/seed_demo.py
    .venv/Scripts/python app.py --demo
"""

import os
import sys

# Allow running from repo root or from scripts/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db

DB_PATH = "jobs.demo.db"

# ---------------------------------------------------------------------------
# Listing definitions
# ---------------------------------------------------------------------------
# Each dict is passed directly to db.insert_listing(). Fields omitted here
# fall through to the defaults set inside insert_listing() (see db.py).
# ---------------------------------------------------------------------------

LISTINGS = [
    # ── High-score listings (8–9) ──────────────────────────────────────────
    {
        "source": "demo",
        "source_id": "demo-001",
        "title": "Senior Python Developer",
        "company": "Northgate Systems",
        "location": "Austin, TX",
        "description": (
            "We are looking for a Senior Python Developer to join our core platform team. "
            "You will design and maintain REST APIs, write pytest-based test suites, and "
            "collaborate with DevOps on Docker-based deployments. Experience with Flask, "
            "SQL databases, and Git workflows is required. Remote-friendly team."
        ),
        "redirect_url": "https://example.com/jobs/demo-001",
        "created_at": "2026-04-01T09:00:00Z",
        "fetched_at": "2026-04-01T10:00:00Z",
        "posted_at": "2026-04-01T09:00:00Z",
        "score": 9.0,
        "matched_skills": ["Python", "Flask", "REST APIs", "Docker", "Git", "pytest"],
        "missing_skills": ["Kubernetes"],
        "concerns": [],
        "verdict": "Excellent match — strong alignment across all primary skills.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 512,
        "tokens_output": 128,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },
    {
        "source": "demo",
        "source_id": "demo-002",
        "title": "Full Stack Engineer — Remote",
        "company": "Apex Digital",
        "location": "Remote",
        "description": (
            "Apex Digital is hiring a Full Stack Engineer to build internal tools and "
            "customer-facing APIs. Stack includes Python/Flask on the backend, SQL (Postgres), "
            "Docker for local dev, and Git-based CI pipelines. Fully remote position with "
            "async-first culture."
        ),
        "redirect_url": "https://example.com/jobs/demo-002",
        "created_at": "2026-04-02T08:00:00Z",
        "fetched_at": "2026-04-02T09:00:00Z",
        "posted_at": "2026-04-02T08:00:00Z",
        "score": 8.5,
        "matched_skills": ["Python", "Flask", "SQL", "Docker", "Git", "REST APIs"],
        "missing_skills": ["Terraform"],
        "concerns": [],
        "verdict": "Strong match for backend candidates who enjoy full-stack work.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 498,
        "tokens_output": 121,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },
    {
        "source": "demo",
        "source_id": "demo-003",
        "title": "Backend Developer (Python/AWS)",
        "company": "Brightpath Labs",
        "location": "Denver, CO",
        "description": (
            "Brightpath Labs is growing its backend team. This role focuses on Python "
            "microservices, REST API design, SQL data modeling, and Docker containerisation. "
            "You will work closely with data engineering. Hybrid — 2 days on-site in Denver, "
            "rest remote."
        ),
        "redirect_url": "https://example.com/jobs/demo-003",
        "created_at": "2026-04-01T14:00:00Z",
        "fetched_at": "2026-04-01T15:00:00Z",
        "posted_at": "2026-04-01T14:00:00Z",
        "score": 8.0,
        "matched_skills": ["Python", "REST APIs", "SQL", "Docker", "Git"],
        "missing_skills": ["Kafka"],
        "concerns": ["Role requires on-site 2 days/week in Denver"],
        "verdict": "Good match — minor concern about on-site requirement.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 487,
        "tokens_output": 115,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },

    # ── Mid-score listings (5–7) ────────────────────────────────────────────
    {
        "source": "demo",
        "source_id": "demo-004",
        "title": "Software Engineer II",
        "company": "Summit Engineering Co.",
        "location": "Chicago, IL (Hybrid)",
        "description": (
            "Summit Engineering is looking for a Software Engineer II to work on our data "
            "ingestion platform. Python is used for pipeline scripts; the team is moving "
            "toward Kafka and GraphQL. Hybrid role — 3 days/week on-site in Chicago."
        ),
        "redirect_url": "https://example.com/jobs/demo-004",
        "created_at": "2026-04-01T10:00:00Z",
        "fetched_at": "2026-04-01T11:00:00Z",
        "posted_at": "2026-04-01T10:00:00Z",
        "score": 7.0,
        "matched_skills": ["Python", "SQL", "Git"],
        "missing_skills": ["Kafka", "GraphQL"],
        "concerns": ["Role requires on-site 3 days/week"],
        "verdict": "Reasonable fit — missing skills are learnable but on-site is a constraint.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 501,
        "tokens_output": 118,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },
    {
        "source": "demo",
        "source_id": "demo-005",
        "title": "DevOps Engineer",
        "company": "Verdant Solutions",
        "location": "Remote",
        "description": (
            "Verdant Solutions needs a DevOps Engineer to manage CI/CD pipelines, "
            "Docker-based deployments, and Kubernetes clusters. Python scripting is used "
            "for automation. Terraform experience strongly preferred. Fully remote."
        ),
        "redirect_url": "https://example.com/jobs/demo-005",
        "created_at": "2026-04-02T11:00:00Z",
        "fetched_at": "2026-04-02T12:00:00Z",
        "posted_at": "2026-04-02T11:00:00Z",
        "score": 6.5,
        "matched_skills": ["Docker", "Git", "Python"],
        "missing_skills": ["Kubernetes", "Terraform"],
        "concerns": ["Role is primarily infrastructure — limited Python development"],
        "verdict": "Good if willing to learn Kubernetes and Terraform; otherwise a stretch.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 493,
        "tokens_output": 113,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },
    {
        "source": "demo",
        "source_id": "demo-006",
        "title": "Data Platform Engineer",
        "company": "Cloudrise Inc.",
        "location": "Seattle, WA",
        "description": (
            "Cloudrise Inc. is building a unified data platform. The role involves Python ETL "
            "pipelines, SQL data warehousing, and some Kafka stream processing. TypeScript is "
            "used for the admin UI layer. Hybrid — flexible on-site schedule."
        ),
        "redirect_url": "https://example.com/jobs/demo-006",
        "created_at": "2026-04-03T09:00:00Z",
        "fetched_at": "2026-04-03T10:00:00Z",
        "posted_at": "2026-04-03T09:00:00Z",
        "score": 6.0,
        "matched_skills": ["Python", "SQL", "Docker"],
        "missing_skills": ["Kafka", "TypeScript"],
        "concerns": ["Senior title but junior salary range"],
        "verdict": "Solid backend fit — some skill gaps in streaming and UI layer.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 478,
        "tokens_output": 109,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },
    {
        "source": "demo",
        "source_id": "demo-007",
        "title": "Platform Engineer",
        "company": "Meridian Software",
        "location": "Boston, MA",
        "description": (
            "Meridian Software is expanding its platform engineering group. This role spans "
            "Python automation, REST API integrations, Docker packaging, and Terraform-managed "
            "infrastructure. On-site preferred but remote negotiable for strong candidates."
        ),
        "redirect_url": "https://example.com/jobs/demo-007",
        "created_at": "2026-04-02T13:00:00Z",
        "fetched_at": "2026-04-02T14:00:00Z",
        "posted_at": "2026-04-02T13:00:00Z",
        "score": 6.5,
        "matched_skills": ["Python", "REST APIs", "Docker", "Git"],
        "missing_skills": ["Terraform"],
        "concerns": ["On-site preference may limit remote flexibility"],
        "verdict": "Good match on skills — location flexibility is the main uncertainty.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 495,
        "tokens_output": 117,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },
    {
        "source": "demo",
        "source_id": "demo-008",
        "title": "Backend Engineer",
        "company": "Ironhaven Tech",
        "location": "New York, NY",
        "description": (
            "Ironhaven Tech is hiring a Backend Engineer to work on high-throughput APIs "
            "serving financial data. Python and SQL are core; the team uses Docker and Git "
            "extensively. GraphQL is used for some internal tooling. Office in NYC — hybrid."
        ),
        "redirect_url": "https://example.com/jobs/demo-008",
        "created_at": "2026-04-01T16:00:00Z",
        "fetched_at": "2026-04-01T17:00:00Z",
        "posted_at": "2026-04-01T16:00:00Z",
        "score": 7.0,
        "matched_skills": ["Python", "SQL", "Docker", "Git", "REST APIs"],
        "missing_skills": ["GraphQL"],
        "concerns": ["NYC hybrid — commute days not yet specified"],
        "verdict": "Strong skills match — location cadence worth clarifying in interview.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 509,
        "tokens_output": 122,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },
    {
        "source": "demo",
        "source_id": "demo-009",
        "title": "Site Reliability Engineer",
        "company": "Stratford Analytics",
        "location": "Austin, TX",
        "description": (
            "Stratford Analytics is looking for an SRE to own reliability across Python "
            "services. Responsibilities include on-call rotation, Docker-based incident "
            "response, and Kubernetes capacity management. Python scripting proficiency "
            "required; Terraform is a plus."
        ),
        "redirect_url": "https://example.com/jobs/demo-009",
        "created_at": "2026-04-03T08:00:00Z",
        "fetched_at": "2026-04-03T09:00:00Z",
        "posted_at": "2026-04-03T08:00:00Z",
        "score": 5.5,
        "matched_skills": ["Python", "Docker", "Linux", "Git"],
        "missing_skills": ["Kubernetes", "Terraform"],
        "concerns": ["On-call rotation may not suit all candidates"],
        "verdict": "Decent overlap — SRE focus diverges from pure development preferences.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 483,
        "tokens_output": 111,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },
    {
        "source": "demo",
        "source_id": "demo-010",
        "title": "Backend Engineer (Python/Flask)",
        "company": "Pinecrest Data",
        "location": "Remote",
        "description": (
            "Pinecrest Data builds analytics tooling for enterprise clients. This Backend "
            "Engineer role uses Python and Flask for API development, SQL for data access, "
            "and Docker for containerisation. The team is small and fast-moving. Fully remote."
        ),
        "redirect_url": "https://example.com/jobs/demo-010",
        "created_at": "2026-04-03T11:00:00Z",
        "fetched_at": "2026-04-03T12:00:00Z",
        "posted_at": "2026-04-03T11:00:00Z",
        "score": 7.5,
        "matched_skills": ["Python", "Flask", "SQL", "Docker", "Git"],
        "missing_skills": ["GraphQL"],
        "concerns": [],
        "verdict": "Very good fit — remote, Python-first, and aligned with SaaS experience.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 491,
        "tokens_output": 116,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },

    # ── Low-score listings (2–4) ────────────────────────────────────────────
    {
        "source": "demo",
        "source_id": "demo-011",
        "title": "Junior QA Analyst",
        "company": "Northgate Systems",
        "location": "Chicago, IL (Hybrid)",
        "description": (
            "We are looking for a Junior QA Analyst to join our quality team. "
            "Responsibilities include writing manual test cases, regression testing, and "
            "bug reporting. Some Python scripting is a bonus but not required."
        ),
        "redirect_url": "https://example.com/jobs/demo-011",
        "created_at": "2026-04-01T10:00:00Z",
        "fetched_at": "2026-04-01T11:00:00Z",
        "posted_at": "2026-04-01T10:00:00Z",
        "score": 3.0,
        "matched_skills": ["Python"],
        "missing_skills": [],
        "concerns": ["Junior level", "QA role — not a development position"],
        "verdict": "Mismatch — role is primarily manual QA, not software development.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 456,
        "tokens_output": 104,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },
    {
        "source": "demo",
        "source_id": "demo-012",
        "title": "IT Support Specialist",
        "company": "Summit Engineering Co.",
        "location": "Denver, CO",
        "description": (
            "Summit Engineering is seeking an IT Support Specialist to handle internal "
            "helpdesk tickets, desktop setup, and network troubleshooting. "
            "No development experience required."
        ),
        "redirect_url": "https://example.com/jobs/demo-012",
        "created_at": "2026-04-02T09:00:00Z",
        "fetched_at": "2026-04-02T10:00:00Z",
        "posted_at": "2026-04-02T09:00:00Z",
        "score": 2.0,
        "matched_skills": [],
        "missing_skills": [],
        "concerns": ["Not a software engineering role", "On-site required"],
        "verdict": "Mismatch — IT support role with no overlap with target skills.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 441,
        "tokens_output": 99,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },
    {
        "source": "demo",
        "source_id": "demo-013",
        "title": "Salesforce Administrator",
        "company": "Verdant Solutions",
        "location": "New York, NY",
        "description": (
            "Verdant Solutions requires a Salesforce Administrator to manage CRM workflows, "
            "user permissions, and custom object configuration. Salesforce certifications "
            "are required. No software development experience needed."
        ),
        "redirect_url": "https://example.com/jobs/demo-013",
        "created_at": "2026-04-01T12:00:00Z",
        "fetched_at": "2026-04-01T13:00:00Z",
        "posted_at": "2026-04-01T12:00:00Z",
        "score": 2.0,
        "matched_skills": [],
        "missing_skills": [],
        "concerns": ["Salesforce is an explicit anti-preference", "Not a development role"],
        "verdict": "Poor match — Salesforce is an anti-preference and role is non-technical.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 449,
        "tokens_output": 101,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },
    {
        "source": "demo",
        "source_id": "demo-014",
        "title": "Front-End Developer (React)",
        "company": "Cloudrise Inc.",
        "location": "Boston, MA",
        "description": (
            "Cloudrise Inc. is hiring a Front-End Developer to build React-based dashboards "
            "for our data platform. TypeScript and CSS-in-JS are used throughout. "
            "Backend integration via REST APIs. On-site in Boston 4 days/week."
        ),
        "redirect_url": "https://example.com/jobs/demo-014",
        "created_at": "2026-04-02T15:00:00Z",
        "fetched_at": "2026-04-02T16:00:00Z",
        "posted_at": "2026-04-02T15:00:00Z",
        "score": 3.5,
        "matched_skills": ["REST APIs", "Git"],
        "missing_skills": ["React", "TypeScript"],
        "concerns": [
            "Heavy frontend focus",
            "Role requires on-site 4 days/week",
        ],
        "verdict": "Mismatch — role is primarily frontend; on-site requirement is a blocker.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 462,
        "tokens_output": 106,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },
    {
        "source": "demo",
        "source_id": "demo-015",
        "title": "Technical Recruiter",
        "company": "Ironhaven Tech",
        "location": "Remote",
        "description": (
            "Ironhaven Tech is expanding its talent acquisition team. The Technical Recruiter "
            "will source, screen, and close engineering candidates. Understanding of software "
            "development roles is helpful but deep technical skills are not required."
        ),
        "redirect_url": "https://example.com/jobs/demo-015",
        "created_at": "2026-04-03T07:00:00Z",
        "fetched_at": "2026-04-03T08:00:00Z",
        "posted_at": "2026-04-03T07:00:00Z",
        "score": 2.0,
        "matched_skills": [],
        "missing_skills": [],
        "concerns": ["Non-technical role", "No overlap with engineering skills"],
        "verdict": "Mismatch — recruiter role is unrelated to software development.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 438,
        "tokens_output": 97,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },
    # Extra mid-tier listings to round out the dataset
    {
        "source": "demo",
        "source_id": "demo-016",
        "title": "API Integration Engineer",
        "company": "Meridian Software",
        "location": "Remote",
        "description": (
            "Meridian Software connects enterprise software systems via REST and GraphQL APIs. "
            "This role requires Python, REST API design expertise, and SQL for data mapping. "
            "Docker is used for integration test environments. Fully remote."
        ),
        "redirect_url": "https://example.com/jobs/demo-016",
        "created_at": "2026-04-03T13:00:00Z",
        "fetched_at": "2026-04-03T14:00:00Z",
        "posted_at": "2026-04-03T13:00:00Z",
        "score": 7.0,
        "matched_skills": ["Python", "REST APIs", "SQL", "Docker", "Git"],
        "missing_skills": ["GraphQL"],
        "concerns": [],
        "verdict": "Good match — GraphQL gap is minor given strong REST background.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 488,
        "tokens_output": 114,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },
    {
        "source": "demo",
        "source_id": "demo-017",
        "title": "Software Engineer — Data Pipelines",
        "company": "Stratford Analytics",
        "location": "Austin, TX",
        "description": (
            "Stratford Analytics is building next-generation data pipelines. The role "
            "uses Python extensively for ETL, SQL for warehouse access, and Docker for "
            "containerised jobs. Kafka experience is a plus. Hybrid in Austin — 2 days/week."
        ),
        "redirect_url": "https://example.com/jobs/demo-017",
        "created_at": "2026-04-02T16:00:00Z",
        "fetched_at": "2026-04-02T17:00:00Z",
        "posted_at": "2026-04-02T16:00:00Z",
        "score": 6.0,
        "matched_skills": ["Python", "SQL", "Docker", "Git"],
        "missing_skills": ["Kafka"],
        "concerns": ["On-site 2 days/week in Austin"],
        "verdict": "Solid fit for data-adjacent roles — on-site is manageable locally.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 479,
        "tokens_output": 110,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },
    {
        "source": "demo",
        "source_id": "demo-018",
        "title": "Infrastructure Engineer (Python + Docker)",
        "company": "Pinecrest Data",
        "location": "Remote",
        "description": (
            "Pinecrest Data is growing its infrastructure team. This role focuses on "
            "Python-based automation tooling, Docker image management, and Linux system "
            "administration. Terraform and Kubernetes are used for cloud deployments. "
            "Fully remote with quarterly on-sites."
        ),
        "redirect_url": "https://example.com/jobs/demo-018",
        "created_at": "2026-04-01T17:00:00Z",
        "fetched_at": "2026-04-01T18:00:00Z",
        "posted_at": "2026-04-01T17:00:00Z",
        "score": 5.5,
        "matched_skills": ["Python", "Docker", "Linux", "Git"],
        "missing_skills": ["Terraform", "Kubernetes"],
        "concerns": ["Infrastructure-heavy — less application development"],
        "verdict": "Decent overlap — leans infra over development; skill gaps require ramp-up.",
        "seen": 1,
        "model_used": "demo/demo-model",
        "tokens_input": 484,
        "tokens_output": 112,
        "description_source": "full",
        "job_type": "permanent",
        "contract_time": "full_time",
    },
]


def main() -> None:
    # Remove existing demo DB so we always start clean.
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"Removed existing {DB_PATH}")

    # Create the schema via the standard init function.
    db.init_db(db_path=DB_PATH)

    # Insert all listings.
    inserted = 0
    for listing in LISTINGS:
        db.insert_listing(listing, db_path=DB_PATH)
        inserted += 1

    # Print tier summary.
    high = sum(1 for listing in LISTINGS if listing["score"] >= 8)
    mid = sum(1 for listing in LISTINGS if 5 <= listing["score"] < 8)
    low = sum(1 for listing in LISTINGS if listing["score"] < 5)
    print(f"Seeded {inserted} listings into {DB_PATH}")
    print(f"  High (>=8): {high}  Mid (5-7): {mid}  Low (<5): {low}")


if __name__ == "__main__":
    main()
