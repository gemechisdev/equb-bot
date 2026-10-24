# Client UI/UX implementation prompt — Digital Equb

Use this document as the **product + data contract** for building a fully featured
client for Digital Equb. **Platform is not fixed**: implement as a Telegram Mini App,
a progressive web app (PWA), and/or a native APK — same features and payloads either way.

The existing system is a **Telegram bot + MongoDB backend**. Payments stay **manual /
admin-reviewed** (no payment-provider integration required). The client should make
rules, balances, proofs, draws, and history **visible and verifiable** so members do
not depend on a single admin’s memory.

---

## 1. Product goals

1. Let a member answer at any time:
   - How much have I contributed (verified vs still due)?
   - Who received each pool, and can I verify the draw?
   - What is the next draw time and who is still unpaid this round?
2. Let an organizer:
   - Manage members, review proofs, confirm offline payouts, postpone/draw, cancel.
   - See a cycle statement and an audit trail.
3. Preserve trust model of the bot:
   - Provably fair lottery (commit/reveal).
   - No silent overwrite of financial decisions.
   - Offline settlement; the app records state, it does not move money.

---

## 2. Roles

| Role | Who | Capabilities |
|------|-----|----------------|
| **Member** | Active `equb_members` row | View group, status, own dues, submit/resubmit proof, history, verify draw |
| **Organizer / bot admin** | `admins` collection or `ADMIN_IDS` | Everything member can + review proofs, draw/postpone, mark paid, unverify, reconcile, cancel, payment methods, statement export |
| **Visitor** | Not a member | Read-only public group summary if you expose it; otherwise prompt to join via Telegram |

Auth for any non-Telegram client: **Telegram Login Widget / Mini App `initData`** (validate HMAC on server). Map to `telegram_id` as the primary user key everywhere.

---

## 3. Domain model (wire-level shapes)

IDs: Mongo `ObjectId` as **24-char hex string** in JSON. Timestamps: **ISO-8601 UTC** (`...Z`).

### 3.1 Group (`equb_groups`)

```json
{
  "id": "674a1b2c3d4e5f6789012345",
  "chat_id": -1001234567890,
  "group_number": 1,
  "name": "Family Equb",
  "contribution_amount": 500,
  "currency": "ETB",
  "frequency": "weekly",
  "interval_days": 7,
  "restart_mode": "once",
  "status": "open",
  "cycle_number": 1,
  "current_period": 1,
  "seed_hash": "a3f1…",
  "seed": null,
  "draw_at": "2026-10-30T15:00:00Z",
  "draw_state": "scheduled",
  "created_by": 111111111,
  "created_at": "2026-10-01T10:00:00Z",
  "started_at": "2026-10-05T12:00:00Z",
  "completed_at": null
}
```

**`status` enum:** `open` → `active` → `completed` | `cancelled`  
**`draw_state` enum (while active):** `scheduled` | `drawing` | `drawn` | `overdue` | `null`  
**`restart_mode`:** `once` | `auto`  
**`frequency`:** `weekly` | `biweekly` | `monthly` | `<N>d` (e.g. `3d`) with `interval_days` derived  

While a round is open, **`seed` is secret** (null or omitted in member APIs). After draw, seed appears on the **payout** record (and may be cleared on the group). Always expose **`seed_hash`** as soon as the round is scheduled.

### 3.2 Member (`equb_members`)

```json
{
  "group_id": "674a…",
  "telegram_id": 222222222,
  "username": "alice",
  "display_name": "Alice",
  "status": "active",
  "joined_period": 1,
  "received_payout": false,
  "payout_period": null,
  "joined_at": "2026-10-05T12:05:00Z"
}
```

**`status`:** `active` | `removed`  
Mid-cycle join: `joined_period` set; member must clear **all** contributions from period 1…current before becoming eligible.

### 3.3 Contribution (`equb_contributions`)

One row per `(group_id, cycle_number, period, telegram_id)`.

```json
{
  "id": "674b…",
  "group_id": "674a…",
  "cycle_number": 1,
  "period": 2,
  "telegram_id": 222222222,
  "amount": 500,
  "status": "awaiting_review",
  "proof": {
    "type": "photo",
    "content": "<telegram_file_id_or_https_url_or_text>"
  },
  "proof_history": [
    {
      "proof": { "type": "text", "content": "TX-old" },
      "submitted_at": "2026-10-20T09:00:00Z",
      "status_at_archive": "rejected",
      "reviewed_at": "2026-10-20T10:00:00Z",
      "reviewed_by": 111111111,
      "archived_at": "2026-10-21T08:00:00Z"
    }
  ],
  "review_history": [
    { "status": "rejected", "admin_id": 111111111, "at": "2026-10-20T10:00:00Z" },
    { "status": "verified", "admin_id": 111111111, "at": "2026-10-21T11:00:00Z" }
  ],
  "submitted_at": "2026-10-21T08:00:00Z",
  "reviewed_at": "2026-10-21T11:00:00Z",
  "reviewed_by": 111111111
}
```

**`status` enum:**  
`pending` → `awaiting_review` → `verified` | `rejected`  
Also: `cancelled` (group cancelled while still open)  
**Transitions:**  
- Submit proof: only from `pending` | `rejected` → `awaiting_review`  
- Review: only from `awaiting_review` → `verified` | `rejected`  
- Unverify (admin, pre-draw): `verified` → `pending` (reason in `review_history` as `reversed`)

**`proof.type`:** `photo` | `text`  
- `photo`: `content` is a durable URL or storage key the client can display (Telegram `file_id` is OK if the client is Mini App with bot file access; for PWA/APK prefer HTTPS upload to your storage).  
- `text`: free-form transaction reference.

### 3.4 Payout (`equb_payouts`)

Immutable lottery snapshot + payment state.

```json
{
  "id": "674c…",
  "group_id": "674a…",
  "cycle_number": 1,
  "period": 2,
  "telegram_id": 333333333,
  "amount": 5000,
  "status": "pending",
  "seed": "a1b2c3…32_hex_chars",
  "seed_hash": "sha256_hex_of_seed",
  "eligible_ids": [111111111, 222222222, 333333333],
  "paid_at": null,
  "paid_by": null,
  "created_at": "2026-10-30T15:00:01Z"
}
```

**`status`:** `pending` | `paid`  
**Verify algorithm (must match server):**

```text
expected_hash = SHA256(utf8(seed)).hexdigest()
assert expected_hash == seed_hash
winner = sorted(eligible_ids)[ Random(seed).randrange(len(eligible_ids)) ]
# Python: random.Random(seed) where seed is the string; same as bot
assert winner == telegram_id
```

Expose a **Verify** UI that runs this client-side and/or calls `GET /payouts/{id}/verify`.

### 3.5 Cycle statement (aggregated, not a collection)

Shape returned by ledger logic (see bot `services/ledger_service.py`):

```json
{
  "group_id": "674a…",
  "group_name": "Family Equb",
  "cycle_number": 1,
  "currency": "ETB",
  "contribution_amount": 500,
  "periods": [1, 2, 3],
  "status_counts": { "verified": 12, "pending": 2, "awaiting_review": 1, "rejected": 0 },
  "expected_contributions": 15,
  "verified_contributions": 12,
  "verified_amount_total": 6000,
  "promised_vs_received": {
    "expected_in": 7500,
    "verified_in": 6000,
    "disbursed_out": 5000,
    "pending_out": 5000
  },
  "members": [
    {
      "telegram_id": 222222222,
      "display_name": "Alice",
      "username": "alice",
      "expected": 3,
      "verified": 2,
      "pending": 1,
      "awaiting_review": 0,
      "rejected": 0,
      "verified_amount": 1000,
      "periods_verified": [1, 2],
      "periods_open": [3],
      "received_payout": false,
      "payout_period": null
    }
  ],
  "payouts": [
    {
      "period": 1,
      "telegram_id": 111111111,
      "amount": 5000,
      "status": "paid",
      "seed_hash": "…",
      "eligible_count": 10
    }
  ]
}
```

### 3.6 Audit event (`audit_events`)

Append-only.

```json
{
  "id": "674d…",
  "action": "contribution_verified",
  "actor_id": 111111111,
  "group_id": "674a…",
  "chat_id": -1001234567890,
  "target_telegram_id": 222222222,
  "contribution_id": "674b…",
  "payout_id": null,
  "period": 2,
  "cycle_number": 1,
  "details": { "amount": 500 },
  "created_at": "2026-10-21T11:00:00Z"
}
```

**Known `action` values (extend carefully, never rewrite old rows):**  
`contribution_verified`, `contribution_rejected`, `contribution_reversed`,  
`draw_completed`, `payout_marked_paid`, `payout_reconciled`, `group_cancelled`, …

### 3.7 Payment method (organizer-configured labels)

```json
{
  "id": "1",
  "name": "Telebirr",
  "details": "09xx xxx xxx — Name",
  "enabled": true
}
```

Shown to members on the contribute screen; not a payment gateway.

---

## 4. Screens & UX flows (platform-agnostic)

### 4.1 Member

| Screen | Purpose | Key UI |
|--------|---------|--------|
| **Home / My Equbs** | List groups where user is active | Status chip, next draw countdown, amount due badge |
| **Group board** | Live round | `seed_hash` (copy), `draw_at` local TZ, paid/unpaid roster, winner only if `draw_state=drawn` |
| **Contribute** | Dues oldest-first | Period cards: pending / awaiting / rejected / verified; CTA upload photo or paste TX id |
| **Proof detail** | After submit | Status timeline from `review_history` |
| **History** | Past payouts | Period, winner, amount, status; **Verify** opens fairness sheet |
| **Statement** | Cycle money view | Expected vs verified vs disbursed; optional CSV share |
| **Profile** | Language, notification prefs | Mirror bot languages: `en`, `am`, `om` |

**Empty / error states:** no group, not started bot (cannot DM), proof rejected with reason, draw overdue (show admin action pending).

### 4.2 Organizer

| Screen | Purpose |
|--------|---------|
| **Review queue** | All `awaiting_review` for current cycle; Approve / Reject with confirm |
| **Draw controls** | Draw now, postpone hours, show overdue |
| **Confirm payout** | Winner, amount, **explicit Confirm** (same as bot dual-confirm) |
| **Members** | Add/remove (respect open vs active rules), back-pay indicator |
| **Audit log** | Reverse chronological events |
| **Settings** | Payment methods CRUD, restart mode display, cancel Equb with confirm + summary |

Destructive actions (**payout**, **cancel**) require an in-UI confirmation step (not a single accidental tap).

### 4.3 Visual / UX principles

- **Trust first:** always show commit hash before draw; never show “next winner”.
- **Money clarity:** amounts always with `currency`; use monospace for hashes and TX ids.
- **Status color language:** pending=neutral, awaiting=amber, verified=green, rejected=red, cancelled=gray, overdue=amber/red.
- **Time:** store UTC; display in user or group timezone (bot default `Africa/Addis_Ababa`).
- **Offline-friendly (PWA/APK):** cache last statement/board; queue proof uploads; mark “pending sync”.
- **Accessibility:** large tap targets for Approve/Reject; don’t rely on color alone for status.
- **Celebration:** optional confetti on “you won” — equivalent to bot private message effect; never imply funds already moved until payout `paid`.

---

## 5. API surface (suggested REST; map 1:1 to bot services)

Base: `https://api.example.com/v1`  
Auth: `Authorization: Bearer <telegram-initData-or-session>`  
All mutations idempotent where the bot is (conditional status transitions).

### 5.1 Read

| Method | Path | Notes |
|--------|------|--------|
| `GET` | `/me` | `{ telegram_id, is_admin, display_name, language }` |
| `GET` | `/groups` | Groups for user |
| `GET` | `/groups/{id}` | Board payload: group + members summary + current contributions + current payout if any |
| `GET` | `/groups/{id}/members` | |
| `GET` | `/groups/{id}/contributions?cycle=&period=&status=` | |
| `GET` | `/groups/{id}/payouts` | |
| `GET` | `/groups/{id}/statement?cycle=` | Statement JSON (§3.5) |
| `GET` | `/groups/{id}/audit?limit=50` | Admin |
| `GET` | `/payouts/{id}/verify` | Server-side verify result |
| `GET` | `/me/dues` | Cross-group pending contributions (bot `/contribute` list) |
| `GET` | `/payment-methods` | Enabled methods |

### 5.2 Write (member)

| Method | Path | Body | Result |
|--------|------|------|--------|
| `POST` | `/groups/{id}/join` | `{}` | Member row; may create back-pay contribution stubs |
| `POST` | `/contributions/{id}/proof` | `{ "type": "photo"\|"text", "content": "…" }` | `202` or `200` updated contribution; `409` if not pending/rejected |

Photo upload alternative: `POST /uploads` → `{ "url": "https://…" }` then pass URL as `content`.

### 5.3 Write (admin)

| Method | Path | Body |
|--------|------|------|
| `POST` | `/groups` | `{ name, contribution_amount, frequency, currency?, restart_mode? }` — or bind to existing Telegram `chat_id` |
| `POST` | `/groups/{id}/start` | `{}` → cycle 1, seed_hash, draw_at |
| `POST` | `/contributions/{id}/review` | `{ "status": "verified"\|"rejected" }` — **409** if not `awaiting_review` |
| `POST` | `/contributions/{id}/unverify` | `{ "reason": "…" }` — **409** if payout exists for period |
| `POST` | `/groups/{id}/draw` | `{ "force": true }` optional — claim + run draw |
| `POST` | `/groups/{id}/postpone` | `{ "hours": 24 }` |
| `POST` | `/groups/{id}/payout/confirm` | `{}` — marks paid + advances; **409** if already paid |
| `POST` | `/groups/{id}/reconcile` | `{}` — paid but not advanced |
| `POST` | `/groups/{id}/cancel` | `{ "confirm": true }` |
| `POST` | `/groups/{id}/remind` | `{}` |

Error body convention:

```json
{ "error": "already_paid", "message": "This round's payout has already been marked as paid." }
```

Use the same error **codes** as the bot (`EqubError` suffixes): `invalid_amount`, `no_payout_record`, `already_paid`, `group_not_active`, …

### 5.4 Realtime (optional)

WebSocket or SSE: `contribution.updated`, `draw.completed`, `payout.paid`, `group.board` — payload = relevant resource id + short summary. Clients refetch board on event.

---

## 6. Mapping to current Telegram bot (compatibility)

Until a REST layer exists, a Mini App can still be useful if it **embeds instructions** and deep-links:

| Client action | Bot equivalent |
|---------------|----------------|
| Open dues | `t.me/<bot>?start=contribute` |
| Join | Group command `/joinequb` or callback `joinequb:<group_id>` |
| Review | `/pending` + `crev:approve:<id>` / `crev:reject:<id>` |
| Confirm payout | `/payout` → `conf:payout:<group_id>` |
| Cancel | `/cancelequb` → `conf:cancel:<group_id>` |

Long-term: extract `services/*` behind HTTP; routers become one client among many. **Do not** duplicate lottery math differently — import or reimplement `pick_winner` / SHA-256 exactly as in §3.4.

---

## 7. Non-goals (for this client generation)

- Payment gateway charge/capture (Telebirr API, cards, etc.)
- Custodial wallets
- Showing a future or “likely” winner before draw
- Editing past payout seeds or eligible sets
- Silent admin overrides without audit rows

---

## 8. Acceptance checklist

- [ ] Member can submit proof and see status transitions without Telegram commands (or via clear deep link).
- [ ] Organizer cannot double-approve or double-payout (UI disables + API 409).
- [ ] Verify screen reproduces winner from `seed` + `eligible_ids` + `seed_hash`.
- [ ] Statement numbers match sum of contribution and payout rows.
- [ ] Cancel shows open vs verified counts and any unpaid drawn winner.
- [ ] All timestamps UTC in API; local in UI.
- [ ] Works on small screens; primary actions reachable with one hand.
- [ ] Language pack at least English; structure for `am` / `om`.

---

## 9. Implementation prompt (copy to your coding agent)

```text
Build a fully featured Digital Equb client from docs/CLIENT_UX_PROMPT.md.

Platform: choose ONE of Telegram Mini App, PWA, or Android APK — keep a shared
UI and data layer so a second platform can reuse the API client and screen model.

Implement:
- Auth via Telegram identity (initData or Login Widget) validated on a backend.
- Member: group list, board, contribute + proof upload, history, verify draw, statement.
- Admin: review queue, draw/postpone, confirm payout, unverify, reconcile, cancel, audit, payment methods.
- Types/interfaces matching the JSON shapes in §3.
- API client matching §5 (mock server allowed first; same paths/codes).
- Strict status-machine UX (disable illegal actions).
- Client-side verify using SHA-256 + seeded RNG compatible with Python random.Random(str).
- No payment-provider integration; proofs are photo/text only.
- i18n-ready strings; default English.

Deliver: runnable app, typed models, mock or real API, and a short README for how
to point it at the Equb backend.
```

---

*Aligned with the Quqube Telegram bot (aiogram + MongoDB): atomic reviews/draws/payouts, audit log, cycle statements, manual admin verification.*
