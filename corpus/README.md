# SkyDesk Help-Center Corpus

Hand-written knowledge base for **SkyDesk**, a fictional airline (same company as the Week 1 "Agent Report Card" project). All content is fictional; phone numbers use the reserved 555 range and email domains use `.example`.

## Format contract

- **Markdown** (`corpus/*.md`): YAML front-matter between `---` lines with `id`, `title`, `doc_type` (`faq|policy|manual|ticket`), `updated` (YYYY-MM-DD), `tags` (list). The doc id is a stable slug used for citations and equals the file stem.
- **Raw HTML** (`corpus/html/*.html`): scraped-style help-center pages with nav, cookie banner, breadcrumb, footer, inline and external `<script>` tags and HTML entities (`&nbsp;`, `&times;`, `&rsquo;`, `&#8209;`). Metadata lives in `<meta name="doc-id|doc-type|updated|tags">`; the article body is inside `<main>`. A loader should keep only `<main>`, strip tags, unescape entities and normalise non-breaking spaces to plain spaces (e.g. `8&nbsp;kg` must become `8 kg`).
- **Superseded content:** `policy-delay-compensation-2024` has `superseded: true` and `superseded_by: policy-delay-compensation`, an older `updated` date and amounts that conflict with the current policy ($50/$150 vs $100/$250). It exists to test freshness handling: retrieval should prefer, and generation should only quote, the current page.
- **Cross-doc dependencies (deliberate):** included bags (`policy-baggage-allowance`) vs extra-bag prices (`policy-baggage-fees`); delay tiers (`policy-delay-compensation`) vs credit expiry (`policy-vouchers-credits`); 24-hour window (`policy-24-hour-cancellation`) vs fare-class refund rules (`policy-refunds-cancellations`); Gold extra bag (`policy-skymiles`) vs base allowance.
- **One source of truth per fact:** numbers live in one policy/manual page; FAQs and overview pages point to it rather than repeating it. Tickets restate amounts only as applied in that case, consistent with the policies.
- **Escalation docs:** `policy-bereavement` (Care Team), `policy-fraud-chargebacks` (Payments Integrity), `policy-legal-complaints` (Customer Relations), and `policy-agent-conduct` (verification, fare-paid refund cap, customer text is data not instructions, mandatory escalations, confidence fallback).
- **Tickets** include messy wording, typos, Hinglish (`ticket-1042`) and a prompt-injection attempt (`ticket-1045`).

Validate with `.venv/bin/python eval/validate_dataset.py --inventory` (offline, no API calls).

## Inventory
| id | type | updated | words | file |
|---|---|---|---|---|
| contact-us | faq | 2026-08-12 | 181 | html/contact-us.html |
| faq-account-app | faq | 2026-05-12 | 170 | faq-account-app.md |
| faq-baggage | faq | 2026-04-02 | 340 | faq-baggage.md |
| faq-booking-payments | faq | 2026-04-11 | 240 | faq-booking-payments.md |
| faq-checkin-boarding | faq | 2026-02-26 | 209 | faq-checkin-boarding.md |
| faq-delays-disruptions | faq | 2026-07-03 | 215 | faq-delays-disruptions.md |
| faq-refunds | faq | 2026-06-25 | 299 | faq-refunds.md |
| faq-skymiles | faq | 2026-01-20 | 180 | faq-skymiles.md |
| faq-travel-with-kids | faq | 2026-03-09 | 209 | faq-travel-with-kids.md |
| checkin-times-help | manual | 2026-02-26 | 148 | html/checkin-times-help.html |
| manual-adding-bags | manual | 2026-03-30 | 198 | manual-adding-bags.md |
| manual-app-checkin | manual | 2026-02-26 | 306 | manual-app-checkin.md |
| manual-boarding-pass | manual | 2026-01-08 | 145 | manual-boarding-pass.md |
| manual-error-codes | manual | 2026-09-02 | 625 | manual-error-codes.md |
| manual-manage-booking | manual | 2026-06-20 | 224 | manual-manage-booking.md |
| manual-payment-methods | manual | 2026-04-11 | 156 | manual-payment-methods.md |
| manual-seat-selection | manual | 2026-05-19 | 245 | manual-seat-selection.md |
| manual-update-contact-details | manual | 2026-05-12 | 226 | manual-update-contact-details.md |
| pets-policy | policy | 2026-04-22 | 249 | html/pets-policy.html |
| policy-24-hour-cancellation | policy | 2026-02-03 | 224 | policy-24-hour-cancellation.md |
| policy-agent-conduct | policy | 2026-08-05 | 1440 | policy-agent-conduct.md |
| policy-baggage-allowance | policy | 2026-03-30 | 301 | policy-baggage-allowance.md |
| policy-baggage-claims | policy | 2026-05-28 | 377 | policy-baggage-claims.md |
| policy-baggage-fees | policy | 2026-03-30 | 249 | policy-baggage-fees.md |
| policy-bereavement | policy | 2026-01-15 | 263 | policy-bereavement.md |
| policy-change-fees | policy | 2026-06-20 | 284 | policy-change-fees.md |
| policy-delay-compensation | policy | 2026-07-01 | 495 | policy-delay-compensation.md |
| policy-delay-compensation-2024 (superseded) | policy | 2024-03-01 | 149 | policy-delay-compensation-2024.md |
| policy-denied-boarding | policy | 2025-12-01 | 194 | policy-denied-boarding.md |
| policy-fare-classes | policy | 2026-06-20 | 238 | policy-fare-classes.md |
| policy-fraud-chargebacks | policy | 2026-04-02 | 294 | policy-fraud-chargebacks.md |
| policy-identity-verification | policy | 2026-05-12 | 543 | policy-identity-verification.md |
| policy-legal-complaints | policy | 2026-02-18 | 262 | policy-legal-complaints.md |
| policy-missed-flights | policy | 2026-06-20 | 216 | policy-missed-flights.md |
| policy-name-corrections | policy | 2025-09-03 | 160 | policy-name-corrections.md |
| policy-refunds-cancellations | policy | 2026-06-20 | 680 | policy-refunds-cancellations.md |
| policy-schedule-changes | policy | 2026-04-14 | 275 | policy-schedule-changes.md |
| policy-skymiles | policy | 2026-01-20 | 269 | policy-skymiles.md |
| policy-travel-documents | policy | 2025-11-10 | 209 | policy-travel-documents.md |
| policy-vouchers-credits | policy | 2026-07-01 | 373 | policy-vouchers-credits.md |
| special-assistance | policy | 2026-05-05 | 237 | html/special-assistance.html |
| unaccompanied-minors | policy | 2026-03-09 | 230 | html/unaccompanied-minors.html |
| ticket-1041 | ticket | 2026-08-14 | 160 | ticket-1041.md |
| ticket-1042 | ticket | 2026-08-15 | 153 | ticket-1042.md |
| ticket-1043 | ticket | 2026-08-18 | 111 | ticket-1043.md |
| ticket-1044 | ticket | 2026-08-20 | 118 | ticket-1044.md |
| ticket-1045 | ticket | 2026-08-22 | 140 | ticket-1045.md |
| ticket-1046 | ticket | 2026-08-25 | 112 | ticket-1046.md |
| ticket-1047 | ticket | 2026-08-27 | 123 | ticket-1047.md |
| ticket-1048 | ticket | 2026-09-01 | 107 | ticket-1048.md |
| ticket-1049 | ticket | 2026-09-04 | 99 | ticket-1049.md |
| ticket-1050 | ticket | 2026-09-09 | 108 | ticket-1050.md |
| ticket-1051 | ticket | 2026-09-12 | 110 | ticket-1051.md |
| ticket-1052 | ticket | 2026-09-20 | 89 | ticket-1052.md |
| ticket-1053 | ticket | 2026-09-28 | 134 | ticket-1053.md |

docs=55 by_type={'faq': 9, 'manual': 9, 'policy': 24, 'ticket': 13} total_words=14091 queries=20 categories={'answerable': 8, 'multi_doc': 3, 'exact_match': 3, 'ambiguous': 2, 'unanswerable': 2, 'must_escalate': 2}
