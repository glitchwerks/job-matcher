# Rubric Eval Comparison — 2026-04-25 (Issue #274)

## Run Metadata
- Commit: `35e9088`
- Provider: `anthropic/claude-haiku-4-5-20251001`
- Seed: `20260424`
- Run at: `2026-04-25T09:43:44`
- Sample (requested): 100 listings (32 high / 36 mid / 32 low)
- Sample (actual): 84 listings (16 high / 36 mid / 32 low)
- Sampled source_ids: `141, 1, 60, 4, 75, 134, 118, 119, 53, 82, 120, 163, 8, 80, 166, 85, 6, 74, 24, 18, 37, 35, 49, 57, 73, 34, 36, 41, 123, 17, 27, 2, 152, 22, 131, 162, 66, 149, 65, 125, 42, 10, 101, 84, 112, 136, 143, 151, 103, 44, 45, 116, 138, 153, 72, 140, 98, 46, 104, 7, 168, 113, 102, 132, 114, 121, 52, 59, 169, 68, 97, 55, 170, 100, 40, 9, 146, 78, 56, 32, 51, 115, 127, 108`

## Decision
- Metric: `required / (required + nice_to_have)` across all successful evaluations
- Threshold (Issue #341): `> 80% → tune`; `≤ 80% → close as "no change"`
- **Aggregate result: 65.3%**
- **RECOMMENDATION: no change needed**

## Per-Tier Breakdown
| Tier | N | % required | % nice-to-have |
|------|---|------------|----------------|
| High | 16 | 48.8% | 51.2% |
| Mid | 34 | 58.6% | 41.4% |
| Low | 32 | 77.0% | 23.0% |

## Per-Listing Results
| source_id | title | tier | old_missing | required | nice_to_have |
|-----------|-------|------|-------------|----------|--------------|
| `141` | Banking Full Stack Software Developer TSCM 43657 | high | 1 | 1 | 0 |
| `1` | Software Engineer | high | 0 | 0 | 0 |
| `60` | Deloitte Cyber Fullstack Senior Engineer/Senior... | high | 14 | 7 | 6 |
| `4` | Software Engineer | high | 8 | 5 | 3 |
| `75` | Data Scientist - FCRM | high | 4 | 1 | 3 |
| `134` | Senior Software Engineer App Infra | high | 3 | 0 | 3 |
| `118` | Software Engineer, MyPay | high | 4 | 1 | 1 |
| `119` | Principal Software Engineer (React/.Net) | high | 7 | 3 | 3 |
| `53` | AI & Machine Learning Engineering Consultant - ... | high | 8 | 7 | 6 |
| `82` | Lead Software Developer | high | 0 | 0 | 0 |
| `120` | Full-Stack Software Engineer (AI-Enhanced Devel... | high | 3 | 1 | 4 |
| `163` | Senior Independent Software Developer | high | 0 | 0 | 0 |
| `8` | Sr. Software Engineer | high | 5 | 5 | 0 |
| `80` | Cyber AI Data Engineer Senior Consultant | high | 19 | 4 | 3 |
| `166` | Tech Lead Databricks Data Engineer | high | 8 | 6 | 4 |
| `85` | Lead Software developer | high | 7 | 0 | 7 |
| `6` | Specialist, Software Engineering | mid | 6 | 5 | 8 |
| `74` | Context Engineer - Manager - Consulting - Locat... | mid | 8 | 5 | 5 |
| `24` | Software Engineer, Full-Stack Developer - LATAM... | mid | 6 | 4 | 3 |
| `18` | Senior Software Engineer, Lion Holdings | mid | 15 | 7 | 9 |
| `37` | Senior Manager, Artificial Intelligence | mid | 3 | 3 | 2 |
| `35` | Lead Full Stack Engineer - PxE Consulting | mid | 5 | 5 | 4 |
| `49` | Product Cybersecurity Engineer - Mako Robotics ... | mid | 10 | 4 | 6 |
| `57` | Physical AI Engineering Consultant - Senior - C... | mid | 12 | FAILED | FAILED |
| `73` | Data Scientist II - FCRM -Screening Capabilities | mid | 4 | 1 | 0 |
| `34` | AI Engineer – Copilot & Agentic Development | mid | 5 | 3 | 4 |
| `36` | Solution Architect - Senior Manager - Consultin... | mid | 8 | 7 | 3 |
| `41` | AI and Distributed Systems Fullstack Engineer -... | mid | 15 | 4 | 3 |
| `123` | Software Engineer (Technical Leadership) – Mach... | mid | 7 | 6 | 1 |
| `17` | Senior Software Engineer, Lion Holdings | mid | 13 | 5 | 11 |
| `27` | Software Development Engineering - Advisor I | mid | 14 | FAILED | FAILED |
| `2` | Software Engineer | mid | 4 | 3 | 3 |
| `152` | Senior Cloud Architect ML AI | mid | 5 | 4 | 3 |
| `22` | Staff Software Engineer - Amazon Connect REMOTE | mid | 16 | 11 | 3 |
| `131` | Senior Software Engineer Cloud Platform | mid | 4 | 4 | 3 |
| `162` | Senior Independent AI Engineer / Architect | mid | 4 | 0 | 3 |
| `66` | Senior Data Engineer | mid | 3 | 3 | 3 |
| `149` | Engineering Manager Data Platform | mid | 12 | 10 | 2 |
| `65` | Analytics and Automation Senior Manager | mid | 10 | 10 | 3 |
| `125` | Software Engineer | mid | 6 | 2 | 0 |
| `42` | AI and Distributed Systems Fullstack Engineer -... | mid | 7 | 7 | 1 |
| `10` | Specialist, Software Engineer | mid | 9 | 4 | 5 |
| `101` | Senior Digital Consultant - 95k. | mid | 4 | 3 | 1 |
| `84` | Solutions Architect | mid | 7 | 6 | 3 |
| `112` | DevOps Engineer Hardware Integration (m/w/d) | mid | 7 | 5 | 5 |
| `136` | Senior Solutions Architect | mid | 7 | 3 | 2 |
| `143` | Software Engineer Payment Operations | mid | 3 | 1 | 3 |
| `151` | Senior Software Engineer II Applied AI and Eval... | mid | 4 | 4 | 3 |
| `103` | Kubernetes Administrator (m/w/d) Softwareentwic... | mid | 2 | 3 | 2 |
| `44` | Engineering Manager - Product Engineering A&A | mid | 11 | 11 | 4 |
| `45` | Cloud Engineer - Manager - Consulting - Locatio... | mid | 6 | 5 | 2 |
| `116` | AI Developer with Python for Customer Care AI P... | mid | 7 | 6 | 3 |
| `138` | Regulatory Manager Senior Regulatory Manager | low | 6 | 8 | 0 |
| `153` | Senior IAM Engineer | low | 6 | 9 | 3 |
| `72` | QA Engineer | low | 3 | 6 | 1 |
| `140` | Business Development Manager Real World Data | low | 7 | 7 | 2 |
| `98` | (Senior) Solution Manager - Contact Center & Co... | low | 9 | 6 | 4 |
| `46` | Controls Engineer | low | 8 | 7 | 3 |
| `104` | Senior Global Integrated B2B Marketing Manager,... | low | 9 | 7 | 3 |
| `7` | Platform Software Engineer | low | 5 | 3 | 1 |
| `168` | 🇩🇪 Senior/Staff Software Engineer PHP, TS, Rust... | low | 12 | 4 | 4 |
| `113` | Senior Projektleiter Anlagenbau (gn) | low | 6 | 5 | 1 |
| `102` | Senior E-Commerce Manager | low | 6 | 10 | 0 |
| `132` | Senior Compliance Manager | low | 10 | 10 | 0 |
| `114` | Senior Projektleiter Anlagenbau (gn) | low | 0 | 6 | 1 |
| `121` | Software Engineer, Credit Line | low | 5 | 1 | 1 |
| `52` | Senior Oracle Architect | low | 14 | 11 | 4 |
| `59` | Network Security Engineer L2 | low | 10 | 9 | 3 |
| `169` | Senior Frontend Developer | low | 30 | 13 | 6 |
| `68` | Sr. Cyber Security Engineer | low | 13 | 5 | 4 |
| `97` | Project Engineer Battery Testing (m/w/d) | low | 5 | 5 | 1 |
| `55` | CONFIDENTIAL- Senior Director Technology | low | 8 | 7 | 0 |
| `170` | Online Data Analyst (United States/Spanish spea... | low | 0 | 4 | 1 |
| `100` | Senior Grid Connection Manager | low | 7 | 6 | 1 |
| `40` | Lead E Commerce Engineering - AEM | low | 5 | 4 | 2 |
| `9` | Platform Software Engineer | low | 5 | 4 | 2 |
| `146` | Alignment Engineer | low | 5 | 6 | 0 |
| `78` | Electrical Engineer | low | 11 | 11 | 4 |
| `56` | Principal System Engineer (HYBRID) | low | 8 | 8 | 2 |
| `32` | eCommerce Software EngineerFort Lauderdale, FLH... | low | 5 | 3 | 1 |
| `51` | Electrical Engineer | low | 12 | 12 | 1 |
| `115` | Senior Projektleiter Anlagenbau (gn) | low | 5 | 6 | 1 |
| `127` | Senior Solution Consultant | low | 8 | 4 | 2 |
| `108` | (Senior) Performance Marketing Manager - Paid S... | low | 8 | 7 | 5 |
