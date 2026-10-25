---
id: ticket-1044
title: "Resolved ticket: payment declined SD-ERR-112"
doc_type: ticket
updated: 2026-08-20
tags: [payment, SD-ERR-112, error code]
---

# Ticket #1044: "SD-ERR-112" when paying

**Channel:** in-app chat

**Customer message:**
> keeps saying SD-ERR-112 payment declined. card is fine, i used it yesterday at walmart. tried like 4 times now. is your app broken

**Agent resolution:**
Explained SD-ERR-112 means the card issuer declined the payment or the 3-D Secure confirmation was not completed; SkyDesk cannot see the bank's reason. Because there had been more than 3 declined attempts, the card was blocked for SkyDesk payments for 24 hours, so the customer could not retry it today. Customer paid successfully with Apple Pay within the 20-minute hold so the fare did not change. Advised them to check with the bank about the decline on the original card.

**Resolution code:** RES-INFO
