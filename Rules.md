# Contact Status Rules

## Status Levels (Priority Order)

| Priority | Status | Meaning |
|---|---|---|
| 3 (highest) | `unsubscribed` | Opted out — never email again |
| 2 | `never_subscribed` | Never gave consent |
| 1 (lowest) | `subscribed` | Opted in — can be emailed |

**Lower consent always wins.** A contact cannot be promoted to a higher status if a lower one exists anywhere in the system.

---

## Hard Unsubscribe (CloudWatch / Unsubscribe Link)

When a contact unsubscribes via email link or AWS SES CloudWatch event:

- A `CampaignEvent` row is created with `event_type='unsubscribe'`
- This acts as a **permanent block** across all lists
- Cannot be overridden by any upload or manual add
- Status is always forced to `unsubscribed` regardless of what is uploaded

---

## Status Resolution Matrix

Used when a contact already exists in another list. The uploaded/desired status is combined with the existing status using this matrix:

| Uploaded Status | Existing Status (other list / CloudWatch) | Final Status |
|---|---|---|
| `subscribed` | `subscribed` | `subscribed` |
| `subscribed` | `never_subscribed` | `never_subscribed` |
| `subscribed` | `unsubscribed` | `unsubscribed` |
| `never_subscribed` | `subscribed` | `never_subscribed` |
| `never_subscribed` | `never_subscribed` | `never_subscribed` |
| `never_subscribed` | `unsubscribed` | `unsubscribed` |
| `unsubscribed` | `subscribed` | `never_subscribed` |
| `unsubscribed` | `unsubscribed` | `unsubscribed` |
| `unsubscribed` | `never_subscribed` | `unsubscribed` |

**Special case — Brand-new contact (no existing record anywhere):**
- Uploaded as `unsubscribed` → stored as `never_subscribed` (they never opted in, so "unsubscribed" doesn't apply)
- Uploaded as `subscribed` or `never_subscribed` → stored as-is

---

## Simple Upload (`upload_campaign_contacts`) Rules

CSV file must have an `email` column. Optional columns: `first_name`, `last_name`, `phone`, `status`.

**Valid `status` column values:**

| Value in CSV | Resolved As |
|---|---|
| `subscribed`, `subscribe`, `yes`, `true`, `1`, `opt_in`, `optin` | `subscribed` |
| `unsubscribed`, `unsubscribe`, `opt_out`, `optout` | `unsubscribed` |
| `never_subscribed`, `never`, `no`, `false`, `0` | `never_subscribed` |
| *(missing or unrecognised)* | defaults to `subscribed` |

**Processing order for each row:**

1. Invalid or blank email → skipped
2. Already exists in this list → skipped (duplicate)
3. Check CloudWatch hard unsubscribe → if found, `existing = unsubscribed`
4. Check highest-priority status from other lists → sets `existing`
5. Apply resolution matrix → determines `final_status`
6. Contact added with `final_status`

---

## Result Example

**List A (existing):**

| Contact | List A Status |
|---|---|
| leo@gmail.com | Never Subscribed |
| raone@gmail.com | Subscribed |
| vinzo@gmail.com | Subscribed |
| zoro@gmail.com | Unsubscribed *(via CloudWatch event)* |
| gojo@gmail.com | Never Subscribed |
| lufy@gmail.com | Unsubscribed *(via CloudWatch event)* |

**Creating List B (CSV upload with `status` column):**

| Contact | Uploaded Status | Existing Status | Final List B Status |
|---|---|---|---|
| leo@gmail.com | `subscribed` | `never_subscribed` (List A) | `never_subscribed` |
| raone@gmail.com | `never_subscribed` | `subscribed` (List A) | `never_subscribed` |
| vinzo@gmail.com | `subscribed` | `subscribed` (List A) | `subscribed` |
| zoro@gmail.com | `subscribed` | `unsubscribed` (CloudWatch) | `unsubscribed` |
| gojo@gmail.com | `never_subscribed` | `never_subscribed` (List A) | `never_subscribed` |
| lufy@gmail.com | `never_subscribed` | `unsubscribed` (CloudWatch) | `unsubscribed` |

---

## Future Campaign Sending

Only contacts with `subscribed` status receive campaign emails.  
`never_subscribed` and `unsubscribed` contacts are **silently excluded** from every send.
