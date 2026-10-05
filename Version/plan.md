# QGISimple Integrator - Complete Development Plan

## Overview

A QGIS plugin that enables:
1. **Email-based GIS data exchange** - Download GIS attachments from email
2. **Data versioning and merging** - Compare and commit changes between datasets
3. **GISimple integration** - Seamless connection to GISimple realm for authenticated users
4. **Standalone operation** - Works independently for users without GISimple

---

## Architecture Principles

| Principle | Description |
|-----------|-------------|
| **Dual-mode operation** | GISimple users authenticate via Keycloak (server-side handles realm connection); standalone users configure manually |
| **Server-side security** | All GISimple realm/Keycloak connection logic lives on GISimple server - plugin only calls GISimple API endpoints |
| **Preserve mail.py** | All existing `mail.py` dialog properties and public APIs remain unchanged |
| **Qt6 only** | No Qt6 WebEngine; use QTextBrowser for HTML preview |
| **No third-party libs** | Use QGIS built-in APIs and Python stdlib; request permission if external lib needed |
| **SQLite for standalone config** | Standalone users store server configs and settings in local SQLite database |
| **Standardized storage** | Follow GISimple sanitization pattern for all file paths |

---

## User Modes

### Mode 1: GISimple User
- Authenticates via GISimple server (which handles Keycloak internally)
- Plugin receives token from GISimple - **never sees Keycloak directly**
- GISimple server provides: email server credentials, group membership, trust status
- Can browse group-shared datasets from GISimple realm
- Trusted by default within their groups

### Mode 2: Standalone User
- Manually configures email server (IMAP/POP3/EWS/Graph)
- Stores configuration in **local SQLite database**:
  - Server configs (host, port, protocol, SSL settings)
  - Trust rules (CIDR ranges, trusted domains)
  - User preferences (download paths, cache TTL)
  - Message cache (headers for offline browsing)
- Uses QGIS credential manager for passwords/tokens only
- No GISimple group features

---

## SQLite Database Schema (Standalone Mode)

```sql
-- Server configurations
CREATE TABLE email_servers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    protocol TEXT NOT NULL,  -- 'imap', 'pop3', 'ews', 'graph'
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    use_ssl INTEGER DEFAULT 1,
    use_tls INTEGER DEFAULT 0,
    username TEXT,
    credential_key TEXT,  -- Reference to QGIS credential manager
    is_default INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);

-- Trust rules
CREATE TABLE trust_rules (
    id INTEGER PRIMARY KEY,
    rule_type TEXT NOT NULL,  -- 'cidr', 'domain', 'email'
    value TEXT NOT NULL,
    enabled INTEGER DEFAULT 1
);

-- Settings
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Message cache
CREATE TABLE message_cache (
    message_id TEXT PRIMARY KEY,
    server_id INTEGER,
    mailbox TEXT,
    from_address TEXT,
    subject TEXT,
    date TEXT,
    size INTEGER,
    is_read INTEGER,
    is_trusted INTEGER,
    attachments_json TEXT,
    cached_at TEXT,
    FOREIGN KEY (server_id) REFERENCES email_servers(id)
);

-- Version history (for Phase II)
CREATE TABLE version_records (
    version_id TEXT PRIMARY KEY,
    timestamp TEXT,
    user TEXT,
    client_source TEXT,
    host_layer TEXT,
    host_type TEXT,  -- 'geopackage', 'postgis'
    host_path TEXT,
    operation_type TEXT,
    feature_mappings_json TEXT,
    field_mappings_json TEXT,
    diff_summary_json TEXT,
    status TEXT,
    backup_reference TEXT,
    undo_of TEXT
);
```

---

## Sanitization Rules (GISimple Standard)

Following `sanitizeUploadIdentifier` from GISimple:

```python
def sanitize_identifier(value: str) -> str:
    """
    GISimple-compatible sanitization:
    - Trim whitespace
    - Lowercase
    - Replace spaces with underscore
    - Replace @ with underscore
    - Allow only [a-z0-9._-]
    - Remove all other characters
    """
    if not value:
        return ''
    result = value.strip().lower()
    result = result.replace(' ', '_')
    result = result.replace('@', '_')
    result = re.sub(r'[^a-z0-9._-]', '', result)
    return result
```

---

## Storage Paths (GISimple Standard)

**Plugin Data Directory:**
```
<QGIS_PLUGIN_DATA_DIR>/qgisimple_integrator/
├── config.db                    # SQLite database (standalone mode)
├── email_attachments/
│   └── <sanitized_sender>/
│       └── <YYYYMMDD>_<sanitized_message_id>/
│           └── <sanitized_filename>
└── cache/
    └── headers.db               # Message header cache
```

**Backup Directory (Phase II):**
```
<Desktop>/QGIS_EmailPlugin_Backups/
└── <sanitized_sender>/
    └── <sanitized_host_layer>_backup_<YYYYMMDDHHMMSS>_<version_id>.geojson
```

---

## Phase I: Email Reader and Attachment Downloading

### I.1 - Adapter System

| Component | Status | Purpose |
|-----------|--------|---------|
| **Adapter Interface** | Core | Abstract base class defining: `connect()`, `list_mailboxes()`, `list_messages()`, `fetch_message()`, `fetch_attachment()`, `mark_as_read()`, `disconnect()` |
| **IMAP Adapter** | Core | Python `imaplib` with SSL/TLS, OAuth2 bearer tokens, username/password |
| **POP3 Adapter** | Core | Python `poplib` with SSL/TLS |
| **EWS Adapter** | **EXPERIMENTAL** | Exchange Web Services (may require `exchangelib`) |
| **Microsoft Graph Adapter** | **EXPERIMENTAL** | REST API calls using `urllib` |
| **Protocol Factory** | **EXPERIMENTAL** | Auto-selects adapter; DNS SRV discovery |

**Standardized Message Metadata:**
```python
@dataclass
class MessageMetadata:
    message_id: str
    from_address: str
    subject: str
    date: datetime
    is_read: bool
    mailbox: str
    size: int
    attachments: List[AttachmentMetadata]

@dataclass
class AttachmentMetadata:
    attachment_id: str
    filename: str
    size: int
    content_type: str
```

**Standardized Error Codes:**
- `CONNECTION_FAILED`, `AUTHENTICATION_FAILED`, `MAILBOX_NOT_FOUND`
- `MESSAGE_NOT_FOUND`, `NETWORK_ERROR`, `TIMEOUT`, `SSL_ERROR`, `PERMISSION_DENIED`

---

### I.2 - Security and Trust

| Component | Purpose |
|-----------|---------|
| **GISimple Auth Client** | Calls GISimple server API for authentication - **never touches Keycloak directly** |
| **Credential Storage** | GISimple users: token from GISimple; Standalone users: QGIS credential manager |
| **Trust Evaluator** | Evaluates sender trust based on configured mode |

**GISimple Authentication Flow (Server-Side Security):**
```
┌─────────────┐      ┌─────────────────┐      ┌──────────────┐
│ QGIS Plugin │ ───► │ GISimple Server │ ───► │ Keycloak     │
│             │      │ (handles auth)  │      │ (hidden)     │
└─────────────┘      └─────────────────┘      └──────────────┘
       │                     │
       │ 1. POST /api/auth/login (user/pass)
       │ ──────────────────►│
       │                     │ (server validates with Keycloak)
       │ 2. Token + user info│
       │ ◄──────────────────│
       │                     │
       │ 3. GET /api/email/config (with token)
       │ ──────────────────►│
       │                     │
       │ 4. Email server credentials
       │ ◄──────────────────│
```

**Trust Modes:**

| Mode | Behavior |
|------|----------|
| **Trusted Accounts** | GISimple server provides trusted user list; match `from_address` |
| **Network Trusted** | Trust senders from servers in configured CIDR ranges |
| **Trust All** | Trust every sender (no confirmation prompts) |

**Non-trusted sender behavior:** Flag in UI; require explicit confirmation before download.

---

### I.3 - Attachment Handling

**Allowed Extensions:**
- Top-level: `.zip`, `.geojson`, `.gpkg`, `.kml`
- Inside zips: `.geojson`, `.gpkg`, `.kml`, shapefile sets (`.shp`, `.shx`, `.dbf`, `.prj`, `.cpg`)

**Zip Extraction Rules:**
- Recursively extract nested zips
- Validate shapefile completeness: require `.shp` + `.dbf` + `.shx`
- Reject unsupported files; list rejection reasons in UI

**File Permissions:** User-only where OS supports (mode 0o600).

**Collision Handling:** Append `_1`, `_2`, etc.

---

### I.4 - Email Browser Panel UI

```
┌─────────────────────────────────────────────────────────────────┐
│ Toolbar: [Server ▼] [Connect] [Disconnect] [Refresh] [Settings] │
├──────────┬──────────────────────────────┬───────────────────────┤
│ Mailbox  │ Message List                 │ Preview & Attachments │
│ List     │ From | Subject | Date | Size │ Safe HTML preview     │
│          │ IsRead | Trusted             │ Attachment list       │
│ INBOX    │ ─────────────────────────────│ [Download] [Open]     │
│ Sent     │ john@... | Report | 3/8 | 2M │                       │
│ Drafts   │ ✓ Read  | ✓ Trusted          │                       │
├──────────┴──────────────────────────────┴───────────────────────┤
│ Filters: [All ▼] [Read/Unread ▼] [Trusted ▼] [Search...]        │
└─────────────────────────────────────────────────────────────────┘
```

**Features:**
- Dockable panel in QGIS
- Multi-select for batch operations
- Progress bars with cancellation
- Background threading for all network operations

---

### I.5 - Attachment Selection Dialog

- Checkboxes for each attachment
- Single-click preview (GeoJSON/KML text, GPKG layers, shapefile metadata)
- Buttons: **Download**, **Download + Open in QGIS**

---

### I.6 - Settings Dialog

| Tab | Settings |
|-----|----------|
| **Servers** | Add/edit/remove email server configs (stored in SQLite for standalone) |
| **GISimple** | GISimple server URL only (server handles Keycloak internally) |
| **Trust** | Trust mode selection, CIDR list editor |
| **Storage** | Download directory override, max attachment size |
| **Extraction** | Auto-extract zips, shapefile validation strictness |
| **Cache** | Message header cache TTL, clear cache button |

---

### I.7 - Background Processing

- All network and extraction tasks run in `QThread` or `QgsTask`
- Progress bars in UI
- Cancellation support
- Retry policy with configurable count and exponential backoff

---

### I.8 - Message State and Caching

- `mark_as_read()` via adapter
- Option: auto-mark as read on download (configurable)
- Cache message headers in SQLite for offline browsing
- Configurable TTL for cache

---

## Phase II: Data Versioning and Commit Changes

### II.1 - Definitions

| Term | Meaning |
|------|---------|
| **Client** | Incoming dataset (from email or GISimple) |
| **Host** | Dataset to update (GeoPackage or PostGIS) |

---

### II.2 - Host Connectors

| Connector | Methods |
|-----------|---------|
| **GeoPackage** | `list_layers()`, `get_layer()`, `begin_transaction()`, `commit_transaction()`, `rollback_transaction()`, `update_features()`, `insert_features()`, `delete_features()` |
| **PostGIS** | Same interface, using PostgreSQL transactions |

---

### II.3 - Data Loader

**Supported Formats:** GeoJSON, GeoPackage, KML, Shapefile sets

**Normalization:**
```python
@dataclass
class NormalizedFeature:
    feature_id: str
    geometry: QgsGeometry
    attributes: Dict[str, Any]
    crs: QgsCoordinateReferenceSystem
    bbox: QgsRectangle
    source_layer_name: str
    source_path: str
```

For shapefiles missing `.prj`: prompt user to set CRS.

---

### II.4 - Matching Engine

**Smart Match (prioritized):**
1. Unique ID field match (if configured)
2. Attribute match on configurable key fields (case-insensitive)
3. Spatial nearest match within configurable tolerance
4. Bounding box overlap threshold

**Manual Match:** User maps client features to host features manually.

---

### II.5 - Two-Panel UI Dialog

```
┌─────────────────────────────────────────────────────────────────────────┐
│ [Smart Match ▼] [Field Mapping...] Threshold: [====75%====] Tolerance: [10m] │
├─────────────────────────────────┬───────────────────────────────────────┤
│ CLIENT Dataset                  │ HOST Dataset                          │
│ ┌─────────────────────────────┐ │ ┌─────────────────────────────────┐   │
│ │ Map Preview                 │ │ │ Map Preview                     │   │
│ └─────────────────────────────┘ │ └─────────────────────────────────┘   │
│ ┌─────────────────────────────┐ │ ┌─────────────────────────────────┐   │
│ │ Attribute Table             │ │ │ Attribute Table                 │   │
│ └─────────────────────────────┘ │ └─────────────────────────────────┘   │
├─────────────────────────────────┴───────────────────────────────────────┤
│ [Attribute Diff...] [Geometry Diff...] [Commit Changes] [Undo] [Redo]   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

### II.6 - Commit Changes Workflow

**On "Commit Changes" button press:**

1. **Create backup** of affected host features
   - Format: GeoJSON
   - Location: `<Desktop>/QGIS_EmailPlugin_Backups/<sanitized_sender>/`
   - Filename: `<sanitized_host_layer>_backup_<YYYYMMDDHHMMSS>_<version_id>.geojson`

2. **Begin transaction** on host connector

3. **Apply updates** (insert/update/delete)

4. **On success:** Commit transaction, create version record, push to undo stack

5. **On failure:** Rollback transaction, surface error, no version record

---

### II.7 - Undo/Redo System

| Action | Behavior |
|--------|----------|
| **Undo** | Restore host state from GeoJSON backup; create new version record |
| **Redo** | Re-apply commit by replaying stored diffs; create new version record |

---

### II.8 - Version Records

Stored in SQLite (standalone) or PostGIS/GeoPackage metadata table.

| Field | Type |
|-------|------|
| `version_id` | UUID |
| `timestamp` | datetime |
| `user` | string |
| `client_source` | string (email ID + sender, or `gisimpleserver://<dataset_id>`) |
| `host_layer` | string |
| `operation_type` | enum: `attribute-only`, `geometry-update` |
| `feature_mappings` | JSON |
| `field_mappings` | JSON |
| `diff_summary` | JSON |
| `status` | enum: `committed`, `rolled_back` |
| `backup_reference` | path |
| `undo_of` | optional UUID |

---

### II.9 - Conflict Detection

**Detection:** Compare host current values to snapshot; use `last_modified` if available.

**Resolution Options:** Overwrite, Merge, Skip, Create new feature.

---

## Phase III: GISimple Realm Integration

### III.1 - Server-Side Architecture (Security)

**Key Principle:** Plugin NEVER connects to Keycloak directly. All realm/auth logic is on GISimple server.

**GISimple Server Endpoints (to be added):**

| Endpoint | Purpose |
|----------|---------|
| `POST /api/auth/login` | Authenticate user, return token |
| `GET /api/auth/user` | Get current user info + groups |
| `GET /api/email/config` | Get email server config for user |
| `GET /api/email/trusted-senders` | Get list of trusted senders |
| `GET /api/groups` | List user's groups |
| `GET /api/groups/:id/datasets` | List datasets in a group |
| `GET /api/datasets/:id` | Get dataset metadata |
| `GET /api/datasets/:id/download` | Download dataset file |

**Plugin only knows:**
- GISimple server URL
- Bearer token (received from login)
- User info (received from server)

**Plugin NEVER knows:**
- Keycloak URL
- Keycloak realm
- Keycloak client credentials
- How authentication works internally

---

### III.2 - GISimple Integration Tabs

Email Browser Panel now has **three tabs**:

```
┌─────────────────────────────────────────────────────────────────┐
│ [Email] [GISimple Groups] [GISimple Uploads]                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Tab 1: Email - Browse email messages and attachments           │
│  Tab 2: GISimple Groups - Browse group datasets                 │
│  Tab 3: GISimple Uploads - Browse uploaded files from users     │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

**GISimple Groups Tab:**
- Lists groups the user belongs to
- Shows datasets shared in each group
- Download datasets or use as client for versioning

**GISimple Uploads Tab:**
- Lists files uploaded by user and group members
- Filter by group
- Download files or use as client for versioning
- Shows: Filename, Owner, Size, Modified date, Group

---

### III.3 - Trust Propagation and Auto-Trust

**Trusted Emails from GISimple:**
- New endpoint: `GET /api/trusted-emails`
- Returns all email addresses of users in the same groups
- Plugin automatically adds these to local trusted senders
- When user logs into GISimple, group members are auto-trusted

**Trust Sync Workflow:**
1. User authenticates with GISimple
2. Plugin calls `/api/trusted-emails`
3. Receives list of all group members' emails
4. Adds them to local trust rules
5. Email attachments from group members require no confirmation

---

### III.4 - Uploaded Files Integration

**Download from GISimple Uploads:**
- Uses existing `/api/upload-other-data/files` endpoint
- Uses existing `/api/upload-other-data/download/:owner/:filename` endpoint
- Access control: User sees own uploads + group members' uploads
- Files can be downloaded for versioning/merging workflow

---

## File Structure

```
qgisimpleversionning/
├── mail.py                         # PRESERVED - existing dialog
├── __init__.py                     # Plugin entry point
├── metadata.txt                    # QGIS plugin metadata
├── plan.md                         # This development plan
│
├── adapters/
│   ├── __init__.py
│   ├── base.py                     # Abstract adapter interface
│   ├── imap_adapter.py             # IMAP implementation (Core)
│   ├── pop3_adapter.py             # POP3 implementation (Core)
│   ├── ews_adapter.py              # EWS implementation (EXPERIMENTAL)
│   ├── graph_adapter.py            # Microsoft Graph (EXPERIMENTAL)
│   └── factory.py                  # Protocol factory (EXPERIMENTAL)
│
├── security/
│   ├── __init__.py
│   ├── credentials.py              # QGIS credential manager wrapper
│   ├── gisimple_auth.py            # GISimple server auth client
│   └── trust.py                    # Trust evaluator
│
├── storage/
│   ├── __init__.py
│   ├── database.py                 # SQLite database manager
│   ├── sanitizer.py                # GISimple-compatible sanitization
│   ├── extractor.py                # Zip extraction + validation
│   └── cache.py                    # Message header cache
│
├── connectors/
│   ├── __init__.py
│   ├── base.py                     # Abstract host connector
│   ├── geopackage.py               # GeoPackage connector
│   └── postgis.py                  # PostGIS connector
│
├── versioning/
│   ├── __init__.py
│   ├── loader.py                   # Data loader + normalization
│   ├── matcher.py                  # Smart/Manual matching engine
│   ├── differ.py                   # Attribute/geometry diff
│   ├── committer.py                # Commit workflow + backup
│   └── history.py                  # Version records + undo/redo
│
├── gisimple/
│   ├── __init__.py
│   ├── client.py                   # GISimple API client
│   ├── groups.py                   # Group dataset browsing
│   └── uploads.py                  # Uploaded files browsing
│
├── ui/
│   ├── __init__.py
│   ├── email_browser_panel.py      # Dockable Email Browser
│   ├── attachment_dialog.py        # Attachment Selection Dialog
│   ├── settings_dialog.py          # Settings Dialog
│   ├── versioning_dialog.py        # Two-Panel Versioning Dialog
│   ├── conflict_dialog.py          # Conflict Resolution Dialog
│   └── widgets.py                  # Reusable UI components
│
└── utils/
    ├── __init__.py
    ├── errors.py                   # Standardized error codes
    ├── logging.py                  # Operation logging
    └── background.py               # QThread/QgsTask management
```

---

## Milestones Summary

| Milestone | Deliverables |
|-----------|--------------|
| **M1** | Plugin bootstrap, adapter interface, IMAP adapter, basic Email Browser UI, SQLite database, sanitized storage |
| **M2** | GISimple auth client (server-side security), trust evaluator, QGIS credential manager integration |
| **M3** | POP3 adapter, EWS adapter (experimental), Graph adapter (experimental), nested zip extraction, shapefile validation |
| **M4** | Data loader, GeoPackage/PostGIS connectors, Two-Panel UI, Smart Match |
| **M5** | Commit workflow with backup, undo/redo, conflict resolution |
| **M6** | GISimple group browsing, group dataset downloads, integration with Phase II |
| **M7** | Testing, documentation |

---

## Third-Party Library Considerations

| Library | Purpose | Status |
|---------|---------|--------|
| `exchangelib` | EWS adapter | **Needs permission** - EXPERIMENTAL feature |
| `msal` | Microsoft Graph OAuth | **Needs permission** - EXPERIMENTAL feature |

All other functionality uses Python stdlib and QGIS built-in APIs.

---

## GISimple Server API Additions Required

The following endpoints need to be added to GISimple server to support this plugin:

```javascript
// Authentication (may already exist)
POST /api/auth/login          // { username, password } → { token, user }
GET  /api/auth/user           // → { email, groups, roles }

// Email configuration
GET  /api/email/config        // → { servers: [...], default_server_id }
GET  /api/email/trusted       // → { trusted_emails: [], trusted_domains: [] }

// Groups and datasets
GET  /api/groups              // → { groups: [{ id, name, members }] }
GET  /api/groups/:id/datasets // → { datasets: [{ id, name, format, size, owner }] }
GET  /api/datasets/:id        // → dataset metadata
GET  /api/datasets/:id/download // → file stream
```

---

## Implementation Order

### Phase I - Milestone 1 (First Implementation)
1. Create `__init__.py` - Plugin entry point
2. Create `metadata.txt` - QGIS plugin metadata
3. Create `utils/errors.py` - Error codes
4. Create `storage/sanitizer.py` - GISimple-compatible sanitization
5. Create `storage/database.py` - SQLite database manager
6. Create `adapters/base.py` - Abstract adapter interface
7. Create `adapters/imap_adapter.py` - IMAP implementation
8. Create `ui/email_browser_panel.py` - Basic Email Browser Panel
9. Create `ui/widgets.py` - Reusable UI components

### Phase I - Milestone 2
10. Create `security/credentials.py` - QGIS credential manager
11. Create `security/gisimple_auth.py` - GISimple auth client
12. Create `security/trust.py` - Trust evaluator
13. Create `ui/settings_dialog.py` - Settings Dialog

### Phase I - Milestone 3
14. Create `adapters/pop3_adapter.py` - POP3 implementation
15. Create `adapters/ews_adapter.py` - EWS (EXPERIMENTAL)
16. Create `adapters/graph_adapter.py` - Graph (EXPERIMENTAL)
17. Create `adapters/factory.py` - Protocol factory (EXPERIMENTAL)
18. Create `storage/extractor.py` - Zip extraction + validation
19. Create `ui/attachment_dialog.py` - Attachment Selection Dialog
20. Create `utils/background.py` - Background task management
21. Create `storage/cache.py` - Message header cache

### Phase II - Milestone 4
22. Create `versioning/loader.py` - Data loader
23. Create `connectors/base.py` - Abstract host connector
24. Create `connectors/geopackage.py` - GeoPackage connector
25. Create `connectors/postgis.py` - PostGIS connector
26. Create `versioning/matcher.py` - Matching engine
27. Create `ui/versioning_dialog.py` - Two-Panel UI

### Phase II - Milestone 5
28. Create `versioning/differ.py` - Diff engine
29. Create `versioning/committer.py` - Commit workflow
30. Create `versioning/history.py` - Version records + undo/redo
31. Create `ui/conflict_dialog.py` - Conflict Resolution Dialog

### Phase III - Milestone 6
32. Create `gisimple/client.py` - GISimple API client
33. Create `gisimple/groups.py` - Group dataset browsing
34. Update `ui/email_browser_panel.py` - Add GISimple Groups tab

### Milestone 7
35. Create `utils/logging.py` - Operation logging
36. Write tests
37. Write documentation

---

## Notes

- **mail.py** remains unchanged throughout development
- All UI uses Qt6 (no WebEngine)
- All network operations run in background threads
- All file paths use GISimple-compatible sanitization
- Credentials stored securely via QGIS credential manager
- GISimple users never see Keycloak - all auth is server-side
