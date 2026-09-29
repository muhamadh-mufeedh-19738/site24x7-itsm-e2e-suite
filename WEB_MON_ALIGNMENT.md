# Web_Mon Alignment — Single Source of Truth

> **RULE: Web_Mon is the single source of truth for this suite.**
> Before changing any field name, regex, endpoint URL, status integer,
> or delivery-mode mapping in any stage script — verify it against the
> corresponding Java class in Web_Mon first.

---

## 1. Where Ticket IDs Are Stored (The Real Path)

Site24x7 stores ticket IDs in **three places** per delivery:

| Store | Location | Written by | Read by |
|---|---|---|---|
| **Redis** | In-memory key-value store | `ThirdPartyUtil.updateTicketIdInCurrentStatus()` | Recovery (UP) flow — to close/update the right ticket |
| **StatusData** | `WM_CURRENT_STATUS` table (Cassandra) | `ThirdPartyUtil.updateCurrentStatusObject()` | Fallback when Redis is unavailable |
| **AlertLogs / AppLog** | `s247alertlog` AppLog index (Cassandra) | `AlertLogs.addAlertLogToApplog()` | **Your QG suite reads THIS via `/app/api/applog/search/`** |

**Our suite reads from AlertLogs — the correct and only accessible store.**
Redis and StatusData are internal server-side stores with no external API.

---

## 2. Key Fields in the Alert Log Row

Source: `AlertLogs.java` → `addAlertLogToApplog()`

| Field (exact casing) | Type | What it contains | Our script reads it as |
|---|---|---|---|
| `RequestMessageId` | String | **THE TICKET ID** — canonical, written as `jsonObject.put("RequestMessageId", prop.get("ticket_id"))` | `e.get("RequestMessageId") or e.get("requestmessageid")` |
| `CommunicationMode` | Integer | Delivery mode enum (see table below) | `e.get("CommunicationMode") or e.get("communicationmode")` |
| `To` | JSON Array | Integration name — `tpJsonArray.add(prop.get("integration_name"))` | `as_list(e.get("To") or e.get("to"))` |
| `Status` | Integer | Monitor status: 0=DOWN, 1=UP, 2=TROUBLE, 3=CRITICAL | `e.get("Status") or e.get("status")` |
| `Message` | String | Human-readable alert text (also contains "ticket id: X" as text) | Regex fallback only |
| `_zl_timestamp` | Long (epoch ms) | Time the log row was written | Used for windowing |
| `MonitorId` | Long | The monitor this alert belongs to | Filter rows by monitor |
| `DeliveryStatus` | String | Delivery outcome for third-party rows | Detect failed deliveries |

---

## 3. CommunicationMode Integer → Integration Type

Source: `AlertLogs.java` → `getAlertModeName()` + `ThirdPartyServices` enum

| Integer | Tool | Our adapter key |
|---|---|---|
| 1 | Email | (not a ticketing tool) |
| 2 | SMS | (not a ticketing tool) |
| 3 | Voice | (not a ticketing tool) |
| 4 | Chat | (not a ticketing tool) |
| 5 | AlarmsOne | (no adapter) |
| **6** | **ServiceDesk Plus OD (SDPOD)** | **`sdp`** |
| 7 | Slack | (not a ticketing tool) |
| **9** | **PagerDuty** | **`pagerduty`** |
| 10 | SDP On-Premise | (no adapter) |
| 11 | Custom Webhook | (not a ticketing tool) |
| 13 | Microsoft Teams | (not a ticketing tool) |
| **14** | **ServiceNow** | **`servicenow`** |
| 15 | OpsGenie | (no adapter yet) |
| 17 | iLert | (no adapter yet) |
| 18 | JSM Ops | (no adapter yet) |
| 19 | SDPMSP | (no adapter yet) |
| 21 | ConnectWise | (no adapter yet) |
| 22 | Zapier | (not a ticketing tool) |
| 23 | Jira | (no adapter yet) |
| **24** | **Zoho Desk (ZDESK)** | **`zohodesk`** |
| 25 | Zoho Cliq | (not a ticketing tool) |
| 26 | EventBridge | (not a ticketing tool) |
| 27 | Telegram | (not a ticketing tool) |
| 29 | FreshService | (no adapter yet) |
| 30 | VictorOps | (no adapter yet) |
| 31 | FreshDesk | (no adapter yet) |
| 32 | Zendesk | (no adapter yet) |
| 33 | Discord | (not a ticketing tool) |
| **52** | **HaloITSM** | **`halo`** |

---

## 4. How Ticket IDs Flow Into the Alert Log

```
Monitor goes DOWN
      ↓
Notifier (ZohoDesk / PD / SDP / ServiceNow / Halo) calls external API
      ↓
External API returns ticket ID (e.g. "298692000001125072")
      ↓
Notifier calls AlertLogs.addThirdpartyLogs(props, alertType)
  → props.put("ticket_id", ticketId)          ← key field
  → addAlertLogs(props, alertType, ADD)
    → addAlertLogToApplog(props)
      → jsonObject.put("RequestMessageId",     ← THIS IS WHAT WE READ
              (String)prop.get("ticket_id"))
      → AppLogHandler.insertDataToZLogs("s247alertlog", ...)
                                               ← written to Cassandra
```

**Our `stage3_verify.py` reads `RequestMessageId` directly as the PRIMARY
ticket ID source. The regex on `Message` text is a fallback only.**

---

## 5. PagerDuty — Web_Mon Implementation

Source: `PagerDutyNotifier.java`

| Field | Value |
|---|---|
| Endpoint | `https://events.pagerduty.com/generic/2010-04-15/create_event.json` (v1 API) |
| Method | HTTP POST |
| Auth | `service_key` field in JSON body |
| `event_type` | `"trigger"` for DOWN/TROUBLE, `"resolve"` for UP |
| `incident_key` | Unique key per monitor+downtime — used to correlate create and resolve |
| Ticket ID stored | The `incident_key` value (dedup key), NOT a PD incident number |
| Memcache guard | `ThirdPartyServices.PAGERDUTY` — if disabled globally, ALL deliveries silently fail |

---

## 6. SDP OD — API URL

Source: `ThirdPartySettings.java` + `SdpodCmdb.java`

```
https://sdpondemand.manageengine.com/app/{portal}/api/v3/requests
                                          ↑
                                     portal = "itdesk" (default)
```

Our `stage4_tickets.py` SDP adapter uses `self._base()` which resolves to:
```python
f"{self.api}/app/{self.portal}/api/v3"
```
Portal must be set via `SDP_PORTAL=itdesk` in `.itsm.env`.

---

## 7. Files to Check When Making Script Changes

| Change type | Check this Web_Mon file |
|---|---|
| Alert log field names | `source/server/.../reports/AlertLogs.java` → `addAlertLogToApplog()` |
| Delivery mode integers | `AlertLogs.java` → `getAlertModeName()` |
| PagerDuty payload / endpoint | `source/common/.../util/PagerDutyNotifier.java` |
| Integration CRUD (suspend/delete/activate) | `api-src/admin/.../thirdparty/ThirdPartyAPI.java` |
| Monitor status codes | `Constants.java` (0=DOWN, 1=UP, 2=TROUBLE, 3=CRITICAL) |
| SDP OD API path | `source/common/.../util/ThirdPartySettings.java` + `SdpodCmdb.java` |
| Zoho Desk ticket fields | `source/notifiers/ZohoDeskNotifier.java` |
| ServiceNow ticket fields | `source/notifiers/ServiceNowNotifier.java` |

---

## 8. Summary of Alignment Fixes Applied

| Date | Fix | File | Web_Mon source |
|---|---|---|---|
| 2026-09-29 | `RequestMessageId` read as PRIMARY ticket ID source | `stage3_verify.py` | `AlertLogs.java` |
| 2026-09-29 | `CommunicationMode` integer mapping added (MODE_INT_TO_TOOL) | `stage3_verify.py` + `stage4_tickets.py` | `AlertLogs.java` `getAlertModeName()` |
| 2026-09-29 | `To` / `Status` / `Message` field casing fixed (try both cases) | `stage3_verify.py` | `AlertLogs.java` `addAlertLogToApplog()` |
| 2026-09-29 | `pick_tool()` uses integer mode FIRST, name heuristic LAST | `stage4_tickets.py` | `ThirdPartyServices` enum |
| 2026-09-29 | SDP portal path fixed to include `itdesk` | `stage4_tickets.py` | `ThirdPartySettings.java` |
| 2026-09-29 | `"null"` bogus ticket ID discarded | `stage3_verify.py` | `AlertLogs.java` (null guard) |
